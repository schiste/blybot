"""Composition root: builds the object graph and starts the transport.

This is the only module that knows about every layer. Run with
``python -m blybot`` or the ``blybot`` console script.
"""

from __future__ import annotations

import asyncio
import sys
from datetime import timedelta
from functools import partial
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Coroutine

    from blybot.config import Config
    from blybot.domain.ports import Sink, Source, Transform
    from blybot.services.delivery import MessageCollector

from blybot.adapters.discord.author_mask import DiscordAuthorMasker
from blybot.adapters.discord.capabilities import DISCORD_CAPABILITIES
from blybot.adapters.discord.gateway import (
    DiscordGateway,
    DiscordGatewayClient,
    build_gateway_client,
)
from blybot.adapters.discord.transport import DiscordTransport
from blybot.adapters.github.gateway import GitHubRepoGateway
from blybot.adapters.github.issues import GitHubIssueTracker
from blybot.adapters.llm.liftwing import LiftWingClient
from blybot.adapters.mediawiki.publisher import MetaWikiPublisher
from blybot.adapters.system import SystemClock
from blybot.adapters.telegram.admin import AdminHandlers
from blybot.adapters.telegram.analyze import AnalysisHandlers
from blybot.adapters.telegram.app import Lifecycle, Maintenance, run_polling
from blybot.adapters.telegram.capture import CaptureHandlers, HmacAuthorMasker
from blybot.adapters.telegram.handlers import GroupHandlers, PrivateHandlers
from blybot.adapters.telegram.subscribe import SubscriptionHandlers
from blybot.adapters.telegram.token_entry import TokenEntryHandler
from blybot.adapters.telegram.transport import TELEGRAM_CAPABILITIES
from blybot.adapters.toolsdb.archive import ToolsDbArchive
from blybot.adapters.toolsdb.store import PymysqlRunner, ToolsDbStore
from blybot.adapters.toolsdb.subscriptions import ToolsDbSubscriptions
from blybot.config import ConfigurationError, load_config
from blybot.domain.models import LlmSettings
from blybot.domain.ports import StorageError
from blybot.domain.pseudonym import RandomPseudonymFactory
from blybot.domain.sanitizer import WikitextSanitizer
from blybot.observability import Counters, configure_logging, log_event
from blybot.services.analysis_run import AnalysisService
from blybot.services.analyze import (
    ArchiveWindowSource,
    ChatReplySink,
    PromptTransform,
    StatsTransform,
    WikiSectionSink,
    explicit_page_resolver,
)
from blybot.services.binding import TokenBinding
from blybot.services.capture import CaptureReminder, CaptureService
from blybot.services.commands import CommandService
from blybot.services.delivery import message_loop
from blybot.services.directory import ChannelDirectory
from blybot.services.dm_routing import DmRouteRegistry
from blybot.services.engine import ActionEngine
from blybot.services.feedback import FeedbackService
from blybot.services.notify import (
    ChatMessagesSink,
    RepoEventsSource,
    RepoNotifier,
    RuleMatchTransform,
)
from blybot.services.policy import GroupPolicy, SlidingWindowLimiter
from blybot.services.publish import (
    ChatConfirmSink,
    LogPublicationService,
    LogPublishTransform,
)
from blybot.services.repo import GroupRepoService
from blybot.services.schedule import ActionScheduler
from blybot.services.sessions import SessionRegistry
from blybot.services.subscriptions import SubscriptionBinding, SubscriptionScheduler
from blybot.services.transcribe import DmTranscriptionService


def main() -> int:
    """Entry point: load config, then run the platform selected by ``PLATFORM``."""
    try:
        config = load_config()
    except ConfigurationError as exc:
        print(f"configuration error: {exc}", file=sys.stderr)
        return 2

    configure_logging()
    if config.platform == "discord":
        return run_discord(config)
    return run_telegram(config)


def run_telegram(config: Config) -> int:  # noqa: PLR0915 -- the root enumerates the object graph once
    """Build the Telegram object graph and start long polling."""
    counters = Counters()
    clock = SystemClock()
    sanitizer = WikitextSanitizer()
    publisher = MetaWikiPublisher(
        api_url=config.wiki_api_url,
        username=config.wiki_username,
        botpassword=config.wiki_botpassword,
        user_agent=config.user_agent,
        max_attempts=config.wiki_max_retries,
        counters=counters,
    )
    pseudonyms = RandomPseudonymFactory()
    sessions = SessionRegistry(
        pseudonyms=pseudonyms,
        clock=clock,
        ttl=config.session_ttl,
    )
    transcription = DmTranscriptionService(
        publisher=publisher,
        sanitizer=sanitizer,
        sessions=sessions,
        target_page=config.dm_target_base,
        edit_summary=config.edit_summary,
        debounce_seconds=config.burst_debounce.total_seconds(),
        timestamp_granularity=config.timestamp_granularity,
    )
    routes = DmRouteRegistry(clock=clock, route_ttl=config.session_ttl)
    # The key was validated at load; construction can't raise on it.
    store: ToolsDbStore | None = None
    archive: ToolsDbArchive | None = None
    subscriptions_store: ToolsDbSubscriptions | None = None
    capture_service: CaptureService | None = None
    capture_handlers: CaptureHandlers | None = None
    llm_client: LiftWingClient | None = None
    analysis_handlers: AnalysisHandlers | None = None
    llm_defaults: LlmSettings | None = None
    if config.profile_encryption_key:
        runner = PymysqlRunner(
            host=config.toolsdb_host,
            database=config.toolsdb_name,
            cnf_path=Path(config.toolsdb_cnf),
        )
        store = ToolsDbStore(runner=runner, fernet_key=config.profile_encryption_key)
        if config.archive_pseudonym_key:
            archive = ToolsDbArchive(runner=runner)
            subscriptions_store = ToolsDbSubscriptions(runner=runner)
    binding = TokenBinding(clock=clock)
    subscription_binding = SubscriptionBinding(clock=clock)
    gateway = GitHubRepoGateway(user_agent=config.user_agent)
    tracker = (
        GitHubIssueTracker(
            repo=config.github_repo,
            token=config.github_token,
            user_agent=config.user_agent,
        )
        if config.github_token
        else None
    )

    group_policy = GroupPolicy(allowed=set(config.allowed_group_ids))
    directory = ChannelDirectory(
        store=store,
        default_log_page=config.log_target_page,
        default_consent=config.consent_mode,
        # Deliberately NOT config.github_repo: that is the bot's own /bug
        # tracker, not a repo any group consented to bind.
        default_repo="",
        page_suffix=config.wiki_page_suffix,
    )
    log_service = LogPublicationService(
        publisher=publisher,
        sanitizer=sanitizer,
        pseudonyms=pseudonyms,
        clock=clock,
        target_page=config.log_target_page,
        edit_summary=config.edit_summary,
        timestamp_granularity=config.timestamp_granularity,
    )
    # One engine for every deployment tier: the registries grow with the
    # features this deployment enables (v3 phase 5 — every pipeline-shaped
    # behavior runs through the same seam).
    feedback = FeedbackService(tracker) if tracker else None
    sources: dict[str, Source] = {}
    transforms: dict[str, Transform] = {
        "log_publish": LogPublishTransform(service=log_service, page_url_for=config.page_url),
    }
    sinks: dict[str, Sink] = {"chat_confirm": ChatConfirmSink()}
    if feedback is not None:
        sinks["issue_tracker"] = feedback
    if store is not None:
        sources["repo_events"] = RepoEventsSource(store=store, vault=store, gateway=gateway)
        transforms["rule_match"] = RuleMatchTransform(counters=counters)
        sinks["chat_message"] = ChatMessagesSink()
    if store is not None and archive is not None:
        llm_defaults = LlmSettings(lang=config.llm_default_lang)
        llm_client = LiftWingClient(
            api_base=config.liftwing_api_base,
            user_agent=config.user_agent,
            models={
                "default": config.liftwing_model_default,
                "large": config.liftwing_model_large,
            },
            timeout_seconds=config.liftwing_timeout_seconds,
            counters=counters,
        )
        sources["archive_window"] = ArchiveWindowSource(archive=archive)
        transforms["prompt"] = PromptTransform(
            runners={"liftwing": llm_client},
            store=store,
            defaults=llm_defaults,
            max_tokens_ceiling=config.llm_max_tokens_ceiling,
            max_chunks=config.llm_max_chunks_per_run,
            counters=counters,
            max_tokens_per_run=config.llm_max_tokens_per_run,
        )
        transforms["stats"] = StatsTransform()
        sinks["wiki_section"] = WikiSectionSink(
            publisher=publisher,
            sanitizer=sanitizer,
            resolve_page=explicit_page_resolver(directory),
            page_url_for=config.page_url,
            edit_summary=config.edit_summary,
            bot_name=config.bot_name,
        )
        sinks["reply"] = ChatReplySink(max_chars=TELEGRAM_CAPABILITIES.max_message_chars)
    engine = ActionEngine(
        sources=sources, transforms=transforms, sinks=sinks, counters=counters, clock=clock
    )
    subscription_handlers = (
        SubscriptionHandlers(
            profiles=store,
            subscriptions=subscriptions_store,
            binding=subscription_binding,
            default_lang=config.llm_default_lang,
            capabilities=TELEGRAM_CAPABILITIES,
        )
        if subscriptions_store is not None and store is not None
        else None
    )
    subscription_scheduler = (
        SubscriptionScheduler(
            subscriptions=subscriptions_store,
            profiles=store,
            engine=engine,
            clock=clock,
            counters=counters,
            capabilities=TELEGRAM_CAPABILITIES,
        )
        if subscriptions_store is not None and store is not None
        else None
    )
    group_handlers = GroupHandlers(
        engine=engine,
        groups=group_policy,
        limiter=SlidingWindowLimiter(
            clock=clock,
            limit=config.log_throttle_per_minute,
            window=timedelta(minutes=1),
        ),
        directory=directory,
        page_url_for=config.page_url,
        counters=counters,
        group_greeting_text=config.group_greeting_text,
        maintainer=config.maintainer,
        newcomer_welcome_enabled=config.newcomer_welcome_enabled,
        repo_service=(
            GroupRepoService(gateway=gateway, vault=store, directory=directory) if store else None
        ),
        capabilities=TELEGRAM_CAPABILITIES,
        archive=archive,
        subscriptions=subscriptions_store,
        cleanup_delay_seconds=config.log_cleanup_seconds,
        reply_cleanup_delay_seconds=config.reply_cleanup_seconds,
    )

    async def release_clients() -> None:
        await publisher.aclose()
        await gateway.aclose()
        if tracker is not None:
            await tracker.aclose()
        if llm_client is not None:
            await llm_client.aclose()

    private_handlers = PrivateHandlers(
        transcription=transcription,
        sessions=sessions,
        counters=counters,
        directory=directory,
        groups=group_policy,
        routes=routes,
        welcome_text=config.welcome_text,
        dm_page_url=config.page_url(config.dm_target_base),
        maintainer=config.maintainer,
        issues_url=f"https://github.com/{config.github_repo}/issues",
        engine=engine,
        feedback=feedback,
        bug_limiter=SlidingWindowLimiter(
            clock=clock, limit=config.bug_throttle_per_hour, window=timedelta(hours=1)
        ),
        token_entry=TokenEntryHandler(
            binding=binding,
            directory=directory,
            gateway=gateway,
            vault=store,
            counters=counters,
        ),
        subscriptions=subscription_handlers,
    )

    if store is not None and archive is not None:
        capture_service = CaptureService(
            store=store,
            archive=archive,
            limiter=SlidingWindowLimiter(
                clock=clock,
                limit=config.capture_max_per_minute,
                window=timedelta(minutes=1),
            ),
            clock=clock,
            counters=counters,
            max_chars=TELEGRAM_CAPABILITIES.max_message_chars,
            retention_window=timedelta(days=config.capture_retention_days),
        )
        capture_handlers = CaptureHandlers(
            service=capture_service,
            masker=HmacAuthorMasker(key=config.archive_pseudonym_key),
            directory=directory,
            groups=group_policy,
            bot_name=config.bot_name,
        )

    if store is not None and archive is not None:
        analysis_handlers = AnalysisHandlers(
            analysis=AnalysisService(
                engine=engine,
                limiter=SlidingWindowLimiter(clock=clock, limit=6, window=timedelta(hours=1)),
                clock=clock,
                counters=counters,
            ),
            groups=group_policy,
        )

    commands = CommandService(
        directory=directory,
        groups=group_policy,
        page_url_for=config.page_url,
        counters=counters,
        capture_service=capture_service,
    )
    admin_handlers = AdminHandlers(
        directory=directory,
        groups=group_policy,
        counters=counters,
        page_url_for=config.page_url,
        binding=binding,
        vault=store,
        commands=commands,
        archive=archive,
        capture_service=capture_service,
        llm_defaults=llm_defaults,
        llm_max_tokens_ceiling=config.llm_max_tokens_ceiling,
        actions=store if analysis_handlers is not None else None,
        clock=clock,
    )

    notifier = RepoNotifier(store=store, groups=group_policy, engine=engine) if store else None
    scheduler = (
        ActionScheduler(
            store=store, engine=engine, groups=group_policy, clock=clock, counters=counters
        )
        if store is not None and archive is not None
        else None
    )
    bootstrap: Callable[[], Awaitable[None]] | None = None
    if store is not None:
        profile_store, message_archive = store, archive

        subscriptions_bootstrap = subscriptions_store

        async def bootstrap_storage() -> None:
            await profile_store.bootstrap()
            if message_archive is not None:
                await message_archive.bootstrap()
            if subscriptions_bootstrap is not None:
                await subscriptions_bootstrap.bootstrap()

        bootstrap = bootstrap_storage

    reminder = (
        CaptureReminder(
            store=store,
            groups=group_policy,
            clock=clock,
            cadence=timedelta(days=config.capture_reannounce_days),
        )
        if store is not None and archive is not None and config.capture_reannounce_days
        else None
    )
    lifecycle = Lifecycle(
        maintenance=Maintenance(
            sessions=sessions, counters=counters, archive=archive, capture=capture_service
        ),
        transcription=transcription,
        release=release_clients,
        bootstrap=bootstrap,
        notifier=notifier,
        scheduler=scheduler,
        reminder=reminder,
        subscription_scheduler=subscription_scheduler,
        poll_interval_seconds=config.events_poll_minutes * 60,
    )
    run_polling(
        token=config.telegram_bot_token,
        group_handlers=group_handlers,
        private_handlers=private_handlers,
        admin_handlers=admin_handlers,
        lifecycle=lifecycle,
        capture_handlers=capture_handlers,
        analysis_handlers=analysis_handlers,
        subscription_handlers=subscription_handlers,
        capabilities=TELEGRAM_CAPABILITIES,
    )
    return 0


def _spawn(coro: Coroutine[object, object, None]) -> asyncio.Task[None]:
    """Schedule a background coroutine on the running loop (a test seam)."""
    return asyncio.ensure_future(coro)


# Roughly one liveness line every 15 minutes, matching the Telegram cadence.
_DISCORD_HEARTBEAT_SECONDS: Final = 900.0


async def _discord_heartbeat(
    counters: Counters, archive: ToolsDbArchive | None, interval_seconds: float
) -> None:
    """Emit a liveness heartbeat (and archive size) on a fixed cadence.

    Mirrors the Telegram maintenance heartbeat so a *healthy* Discord instance
    is visible in the logs — otherwise "connected and running" is
    indistinguishable from "crash-looping" (see issue #28).
    """
    while True:
        await asyncio.sleep(interval_seconds)
        log_event("heartbeat", "ok", **counters.snapshot())
        if archive is not None:
            try:
                log_event("archive_size", "ok", rows=await archive.total())
            except StorageError:
                log_event("archive_size", "error")


async def _discord_startup(  # noqa: PLR0913 -- setup-hook wiring enumerates its dependencies
    client: DiscordGatewayClient,
    *,
    bootstrap: Callable[[], Awaitable[None]] | None,
    collectors: tuple[tuple[MessageCollector, str], ...],
    poll_interval: float,
    counters: Counters,
    archive: ToolsDbArchive | None,
    heartbeat_interval: float,
) -> None:
    """Client ``setup_hook`` body: bootstrap storage, start the delivery loops.

    The transport wraps the now-live client and drives the shared neutral
    :func:`message_loop` for every collector (subscription digests, capture
    reminders) — the same loop the Telegram lifecycle uses. A liveness
    heartbeat runs alongside, and a final ``startup`` line marks readiness,
    matching the Telegram lifecycle's observability.
    """
    if bootstrap is not None:
        try:
            await bootstrap()
        except StorageError:
            log_event("storage_bootstrap", "error")
    transport = DiscordTransport(client)
    for collector, label in collectors:
        _spawn(message_loop(transport, collector, poll_interval, label))
    _spawn(_discord_heartbeat(counters, archive, heartbeat_interval))
    log_event("startup", "ok")


def discord_run(
    client: DiscordGatewayClient, token: str, release: Callable[[], Coroutine[Any, Any, None]]
) -> None:
    """Start the gateway (blocks for the process lifetime), releasing clients after.

    ``client.run`` owns the event loop and returns only when the bot stops,
    so the HTTP clients are closed afterwards on a fresh loop.
    """
    try:
        client.run(token)
    finally:
        asyncio.run(release())


def run_discord(config: Config) -> int:
    """Build the Discord object graph and start the gateway client.

    Reuses every neutral service (directory, capture, engine, subscription
    scheduler, archive, store) — only the transport and the inbound event
    shell are Discord-specific. Repo notifications and scheduled analyses
    are deferred: the Discord admin surface does not yet configure them.
    """
    counters = Counters()
    clock = SystemClock()
    sanitizer = WikitextSanitizer()
    publisher = MetaWikiPublisher(
        api_url=config.wiki_api_url,
        username=config.wiki_username,
        botpassword=config.wiki_botpassword,
        user_agent=config.user_agent,
        max_attempts=config.wiki_max_retries,
        counters=counters,
    )
    group_policy = GroupPolicy(allowed=set(config.allowed_group_ids))

    store: ToolsDbStore | None = None
    archive: ToolsDbArchive | None = None
    subscriptions_store: ToolsDbSubscriptions | None = None
    if config.profile_encryption_key:
        runner = PymysqlRunner(
            host=config.toolsdb_host,
            database=config.toolsdb_name,
            cnf_path=Path(config.toolsdb_cnf),
        )
        store = ToolsDbStore(runner=runner, fernet_key=config.profile_encryption_key)
        if config.archive_pseudonym_key:
            archive = ToolsDbArchive(runner=runner)
            subscriptions_store = ToolsDbSubscriptions(runner=runner)

    directory = ChannelDirectory(
        store=store,
        default_log_page=config.log_target_page,
        default_consent=config.consent_mode,
        default_repo="",
        page_suffix=config.wiki_page_suffix,
    )

    llm_client: LiftWingClient | None = None
    capture_service: CaptureService | None = None
    masker: DiscordAuthorMasker | None = None
    analysis_service: AnalysisService | None = None
    collectors: list[tuple[MessageCollector, str]] = []
    if store is not None and archive is not None and subscriptions_store is not None:
        llm_client = LiftWingClient(
            api_base=config.liftwing_api_base,
            user_agent=config.user_agent,
            models={
                "default": config.liftwing_model_default,
                "large": config.liftwing_model_large,
            },
            timeout_seconds=config.liftwing_timeout_seconds,
            counters=counters,
        )
        engine = ActionEngine(
            sources={"archive_window": ArchiveWindowSource(archive=archive)},
            transforms={
                "prompt": PromptTransform(
                    runners={"liftwing": llm_client},
                    store=store,
                    defaults=LlmSettings(lang=config.llm_default_lang),
                    max_tokens_ceiling=config.llm_max_tokens_ceiling,
                    max_chunks=config.llm_max_chunks_per_run,
                    counters=counters,
                    max_tokens_per_run=config.llm_max_tokens_per_run,
                ),
                "stats": StatsTransform(),
            },
            sinks={
                # The on-demand analyses publish to the wiki (mirroring
                # Telegram); the reply sink stays for scheduled digest DMs.
                "wiki_section": WikiSectionSink(
                    publisher=publisher,
                    sanitizer=sanitizer,
                    resolve_page=explicit_page_resolver(directory),
                    page_url_for=config.page_url,
                    edit_summary=config.edit_summary,
                    bot_name=config.bot_name,
                ),
                "reply": ChatReplySink(max_chars=DISCORD_CAPABILITIES.max_message_chars),
            },
            counters=counters,
            clock=clock,
        )
        analysis_service = AnalysisService(
            engine=engine,
            limiter=SlidingWindowLimiter(clock=clock, limit=6, window=timedelta(hours=1)),
            clock=clock,
            counters=counters,
        )
        capture_service = CaptureService(
            store=store,
            archive=archive,
            limiter=SlidingWindowLimiter(
                clock=clock, limit=config.capture_max_per_minute, window=timedelta(minutes=1)
            ),
            clock=clock,
            counters=counters,
            max_chars=DISCORD_CAPABILITIES.max_message_chars,
            retention_window=timedelta(days=config.capture_retention_days),
        )
        masker = DiscordAuthorMasker(key=config.archive_pseudonym_key)
        collectors.append(
            (
                SubscriptionScheduler(
                    subscriptions=subscriptions_store,
                    profiles=store,
                    engine=engine,
                    clock=clock,
                    counters=counters,
                    capabilities=DISCORD_CAPABILITIES,
                ),
                "sub_tick",
            )
        )

    if capture_service is not None and store is not None and config.capture_reannounce_days:
        collectors.append(
            (
                CaptureReminder(
                    store=store,
                    groups=group_policy,
                    clock=clock,
                    cadence=timedelta(days=config.capture_reannounce_days),
                ),
                "capture_remind",
            )
        )

    commands = CommandService(
        directory=directory,
        groups=group_policy,
        page_url_for=config.page_url,
        counters=counters,
        capture_service=capture_service,
    )
    gateway = DiscordGateway(
        directory=directory,
        groups=group_policy,
        commands=commands,
        capture=capture_service,
        masker=masker,
        subscriptions=subscriptions_store,
        analysis=analysis_service,
        default_lang=config.llm_default_lang,
    )

    async def release_clients() -> None:
        await publisher.aclose()
        if llm_client is not None:
            await llm_client.aclose()

    bootstrap: Callable[[], Awaitable[None]] | None = None
    if store is not None:
        profile_store, message_archive, subs_store = store, archive, subscriptions_store

        async def bootstrap_storage() -> None:
            await profile_store.bootstrap()
            if message_archive is not None:
                await message_archive.bootstrap()
            if subs_store is not None:
                await subs_store.bootstrap()

        bootstrap = bootstrap_storage

    client = build_gateway_client(
        gateway,
        on_setup=partial(
            _discord_startup,
            bootstrap=bootstrap,
            collectors=tuple(collectors),
            poll_interval=config.events_poll_minutes * 60,
            counters=counters,
            archive=archive,
            heartbeat_interval=_DISCORD_HEARTBEAT_SECONDS,
        ),
    )
    discord_run(client, config.discord_bot_token, release_clients)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
