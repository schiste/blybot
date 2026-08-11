"""Composition root: builds the object graph and starts the transport.

This is the only module that knows about every layer. Run with
``python -m blybot`` or the ``blybot`` console script.
"""

from __future__ import annotations

import asyncio
import sys
from dataclasses import dataclass
from datetime import timedelta
from functools import partial
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Coroutine

    from blybot.config import Config
    from blybot.domain.ports import Sink, Source, Transform, Transport
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
from blybot.adapters.irc.author_mask import IrcAuthorMasker
from blybot.adapters.irc.capabilities import IRC_CAPABILITIES
from blybot.adapters.irc.connection import IrcConnection
from blybot.adapters.irc.gateway import IrcGateway, IrcSession
from blybot.adapters.irc.transport import IrcTransport
from blybot.adapters.llm.liftwing import LiftWingClient
from blybot.adapters.mediawiki.publisher import MetaWikiPublisher
from blybot.adapters.system import SystemClock
from blybot.adapters.telegram.admin import AdminHandlers
from blybot.adapters.telegram.analyze import AnalysisHandlers
from blybot.adapters.telegram.app import (
    Lifecycle,
    Maintenance,
    build_polling_application,
    poll_until_cancelled,
)
from blybot.adapters.telegram.bridge import BridgeHandlers
from blybot.adapters.telegram.capture import CaptureHandlers, HmacAuthorMasker
from blybot.adapters.telegram.handlers import GroupHandlers, PrivateHandlers
from blybot.adapters.telegram.subscribe import SubscriptionHandlers
from blybot.adapters.telegram.token_entry import TokenEntryHandler
from blybot.adapters.telegram.transport import TELEGRAM_CAPABILITIES, TelegramTransport
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
    SubscriberDmSink,
    WikiSectionSink,
    explicit_page_resolver,
)
from blybot.services.binding import TokenBinding
from blybot.services.bridge import BridgeService
from blybot.services.bridge_router import BridgeRouter
from blybot.services.capture import CaptureReminder, CaptureService
from blybot.services.commands import CommandService
from blybot.services.delivery import message_loop
from blybot.services.directory import ChannelDirectory
from blybot.services.dm_routing import DmRouteRegistry, PendingDmMessages
from blybot.services.engine import ActionEngine
from blybot.services.feedback import FeedbackService
from blybot.services.health import heartbeat_loop, log_startup
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
from blybot.services.sessions import SessionRegistry, SessionSweeper
from blybot.services.subscriptions import SubscriptionBinding, SubscriptionScheduler
from blybot.services.transcribe import DmTranscriptionService


def run_unified(config: Config) -> int:
    """Run every configured platform in one process, bridged together (#78).

    The bridge needs the process that *receives* a message to reach the
    platform that must *send* it, and a separate bridging job cannot do
    that — Telegram's getUpdates is exclusive to one poller and a second
    IRC connection collides on the nick. So a mirror requires one process
    hosting every adapter.

    The cost is deliberate and documented: this trades the per-platform
    isolation the default deployment has, where one platform's crash
    cannot touch another's. That is why unified mode is opt-in rather
    than the default.
    """
    counters = Counters()
    router = BridgeRouter(
        bridge=BridgeService(counters=counters, bot_authors=_bot_authors(config)),
        store=_bridge_store(config),
    )
    builders = (
        ("telegram", build_telegram),
        ("discord", build_discord),
        ("irc", build_irc),
    )
    runtimes = [build(config, router) for name, build in builders if _configured(config, name)]
    if not runtimes:
        print("configuration error: unified mode needs at least one platform", file=sys.stderr)
        return 2
    for runtime in runtimes:
        if runtime.transport is not None:
            router.register(runtime.platform, runtime.transport)
    asyncio.run(_run_together(runtimes, router))
    return 0


def _bridge_store(config: Config) -> ToolsDbStore | None:
    """The store the router reads bridge membership from, if configured."""
    if not config.profile_encryption_key:
        return None
    runner = PymysqlRunner(
        host=config.toolsdb_host,
        database=config.toolsdb_name,
        cnf_path=Path(config.toolsdb_cnf),
    )
    return ToolsDbStore(runner=runner, fernet_key=config.profile_encryption_key)


def _bot_authors(config: Config) -> dict[str, str]:
    """The bot's own display name per platform, for the relay echo fence."""
    return {"irc": config.irc_nick, "telegram": config.bot_name, "discord": config.bot_name}


def _configured(config: Config, platform: str) -> bool:
    """Whether this deployment has what the platform needs to start."""
    return bool(
        {
            "telegram": config.telegram_bot_token,
            "discord": config.discord_bot_token,
            "irc": config.irc_server,
        }[platform]
    )


async def _run_together(
    runtimes: list[PlatformRuntime], router: BridgeRouter | None = None
) -> None:
    """Run every platform until one stops, then unwind them all.

    A platform ending is treated as the process ending. The alternative —
    carrying on with a hole in the mirror — is worse: the remaining
    channels would keep relaying to each other while silently dropping
    everything bound for the platform that died.
    """
    tasks = [asyncio.ensure_future(runtime.start()) for runtime in runtimes]
    # The relay drains are background work, not platforms: they never end
    # on their own, so they are cancelled with the rest rather than being
    # something the process waits on.
    drains = [asyncio.ensure_future(worker) for worker in (router.workers() if router else [])]
    try:
        done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
        for task in done:
            task.result()  # re-raise whatever ended the process
    finally:
        for drain in drains:
            drain.cancel()
        await asyncio.gather(*drains, return_exceptions=True)
        for runtime in runtimes:
            if runtime.release is not None:
                await runtime.release()


@dataclass(eq=False)
class PlatformRuntime:
    """One platform, built and ready to run inside someone else's loop.

    Splitting "build the graph" from "start the transport" is what lets a
    single process host several platforms (#78): the isolated per-platform
    jobs still call the blocking entry points, while the unified runtime
    gathers the coroutines instead.
    """

    start: Callable[[], Coroutine[Any, Any, None]]
    release: Callable[[], Awaitable[None]] | None = None
    # Available before the platform is live where the SDK allows it, so the
    # router can reach a platform as soon as it comes up.
    transport: Transport | None = None
    platform: str = ""
    # Set where a platform's SDK offers a better *isolated* entry point than
    # `start`. Telegram's `run_polling` installs the signal handlers that
    # give a Toolforge SIGTERM a graceful shutdown; the unified runtime
    # cannot use it (one loop, three platforms) and provides its own.
    run_alone: Callable[[], None] | None = None


def main() -> int:
    """Entry point: load config, then run the platform selected by ``PLATFORM``."""
    try:
        config = load_config()
    except ConfigurationError as exc:
        print(f"configuration error: {exc}", file=sys.stderr)
        return 2

    configure_logging()
    if config.platform == "unified":
        return run_unified(config)
    if config.platform == "discord":
        return run_discord(config)
    if config.platform == "irc":
        return run_irc(config)
    return run_telegram(config)


def build_telegram(  # noqa: PLR0915 -- the root enumerates the object graph once
    config: Config, bridge: BridgeRouter | None = None
) -> PlatformRuntime:
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
    pending_dms = PendingDmMessages(clock=clock)
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
    # The archive and the subscription store are created together (both
    # need ARCHIVE_PSEUDONYM_KEY), so naming all three keeps the analysis
    # tier's precondition in one place.
    if store is not None and archive is not None and subscriptions_store is not None:
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
        reply_sink = ChatReplySink(max_chars=TELEGRAM_CAPABILITIES.max_message_chars)
        sinks["reply"] = reply_sink
        # `delivery=wiki+subs` / `delivery=subs` (#71): one report,
        # re-addressed to everyone inheriting this channel's digest.
        sinks["subscriber_dm"] = SubscriberDmSink(
            subscriptions=subscriptions_store,
            reply=reply_sink,
            capabilities=TELEGRAM_CAPABILITIES,
            counters=counters,
        )
    engine = ActionEngine(
        sources=sources, transforms=transforms, sinks=sinks, counters=counters, clock=clock
    )
    # One limiter object behind /log, /issue and /repo: the buckets are keyed
    # per command, so sharing it preserves each command's own cap.
    group_limiter = SlidingWindowLimiter(
        clock=clock, limit=config.log_throttle_per_minute, window=timedelta(minutes=1)
    )
    commands = CommandService(
        directory=directory,
        groups=group_policy,
        page_url_for=config.page_url,
        counters=counters,
        capture_service=capture_service,
        vault=store,
        repo_actions=gateway,
        actions=store if archive is not None else None,
        clock=clock,
        engine=engine,
        subscriptions=subscriptions_store,
        capabilities=TELEGRAM_CAPABILITIES,
        default_lang=config.llm_default_lang,
        repo_service=(
            GroupRepoService(gateway=gateway, vault=store, directory=directory) if store else None
        ),
        repo_limiter=group_limiter,
        llm_defaults=llm_defaults,
        llm_max_tokens_ceiling=config.llm_max_tokens_ceiling,
    )
    subscription_handlers = (
        SubscriptionHandlers(
            profiles=store,
            subscriptions=subscriptions_store,
            binding=subscription_binding,
            commands=commands,
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
        limiter=group_limiter,
        directory=directory,
        page_url_for=config.page_url,
        counters=counters,
        group_greeting_text=config.group_greeting_text,
        maintainer=config.maintainer,
        newcomer_welcome_enabled=config.newcomer_welcome_enabled,
        commands=commands,
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
        pending=pending_dms,
        capabilities=TELEGRAM_CAPABILITIES,
        welcome_text=config.welcome_text,
        dm_page_url=config.page_url(config.dm_target_base),
        maintainer=config.maintainer,
        issues_url=f"https://github.com/{config.github_repo}/issues",
        engine=engine,
        feedback=feedback,
        bug_limiter=SlidingWindowLimiter(
            clock=clock, limit=config.bug_throttle_per_hour, window=timedelta(hours=1)
        ),
        token_entry=TokenEntryHandler(binding=binding, directory=directory, commands=commands),
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

    admin_handlers = AdminHandlers(
        directory=directory,
        groups=group_policy,
        counters=counters,
        page_url_for=config.page_url,
        binding=binding,
        commands=commands,
        archive=archive,
        capture_service=capture_service,
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
    application, allowed = build_polling_application(
        token=config.telegram_bot_token,
        group_handlers=group_handlers,
        private_handlers=private_handlers,
        admin_handlers=admin_handlers,
        lifecycle=lifecycle,
        capture_handlers=capture_handlers,
        analysis_handlers=analysis_handlers,
        subscription_handlers=subscription_handlers,
        capabilities=TELEGRAM_CAPABILITIES,
        bridge_handlers=BridgeHandlers(router=bridge) if bridge is not None else None,
    )

    def start() -> Coroutine[Any, Any, None]:
        return poll_until_cancelled(application, allowed)

    return PlatformRuntime(
        start=start,
        transport=TelegramTransport(application.bot),
        platform="telegram",
        run_alone=lambda: application.run_polling(allowed_updates=allowed),
    )


def run_telegram(config: Config) -> int:
    """Build the Telegram object graph and start long polling."""
    runtime = build_telegram(config)
    # Always set for Telegram: `run_polling` installs the signal handlers a
    # Toolforge SIGTERM needs, which the shared async path cannot provide.
    assert runtime.run_alone is not None  # noqa: S101 -- structural, not a check
    runtime.run_alone()
    return 0


def _spawn(coro: Coroutine[object, object, None]) -> asyncio.Task[None]:
    """Schedule a background coroutine on the running loop (a test seam)."""
    return asyncio.ensure_future(coro)


# Roughly one liveness line every 15 minutes, matching the Telegram cadence.
_DISCORD_HEARTBEAT_SECONDS: Final = 900.0
_IRC_HEARTBEAT_SECONDS: Final = 900.0


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
    _spawn(heartbeat_loop(counters, archive, heartbeat_interval))
    log_startup()


def build_discord(  # noqa: PLR0915 -- the root enumerates the object graph once
    config: Config, bridge: BridgeRouter | None = None
) -> PlatformRuntime:
    """Build the Discord object graph and start the gateway client.

    Reuses every neutral service (directory, capture, engine, subscription
    scheduler, repo notifier, action scheduler, archive, store) — only the
    transport and the inbound event shell are Discord-specific.
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

    repo_gateway = GitHubRepoGateway(user_agent=config.user_agent)
    llm_client: LiftWingClient | None = None
    capture_service: CaptureService | None = None
    masker: DiscordAuthorMasker | None = None
    analysis_service: AnalysisService | None = None
    llm_defaults: LlmSettings | None = None
    collectors: list[tuple[MessageCollector, str]] = []
    # One engine for every deployment tier, exactly as on Telegram: the
    # registries grow with the features this deployment enables.
    sources: dict[str, Source] = {}
    # The /log pipeline is available on every tier: it publishes the message
    # it is handed, needing neither the profile store nor the archive.
    transforms: dict[str, Transform] = {
        "log_publish": LogPublishTransform(
            service=LogPublicationService(
                publisher=publisher,
                sanitizer=sanitizer,
                pseudonyms=RandomPseudonymFactory(),
                clock=clock,
                target_page=config.log_target_page,
                edit_summary=config.edit_summary,
                timestamp_granularity=config.timestamp_granularity,
            ),
            page_url_for=config.page_url,
        )
    }
    sinks: dict[str, Sink] = {"chat_confirm": ChatConfirmSink()}
    if store is not None:
        sources["repo_events"] = RepoEventsSource(store=store, vault=store, gateway=repo_gateway)
        transforms["rule_match"] = RuleMatchTransform(counters=counters)
        sinks["chat_message"] = ChatMessagesSink()
    if store is not None and archive is not None and subscriptions_store is not None:
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
        # The on-demand analyses publish to the wiki (mirroring Telegram);
        # the reply sink stays for scheduled digest DMs.
        sinks["wiki_section"] = WikiSectionSink(
            publisher=publisher,
            sanitizer=sanitizer,
            resolve_page=explicit_page_resolver(directory),
            page_url_for=config.page_url,
            edit_summary=config.edit_summary,
            bot_name=config.bot_name,
        )
        reply_sink = ChatReplySink(max_chars=DISCORD_CAPABILITIES.max_message_chars)
        sinks["reply"] = reply_sink
        # `delivery=wiki+subs` / `delivery=subs` (#71): one report,
        # re-addressed to everyone inheriting this channel's digest.
        sinks["subscriber_dm"] = SubscriberDmSink(
            subscriptions=subscriptions_store,
            reply=reply_sink,
            capabilities=DISCORD_CAPABILITIES,
            counters=counters,
        )
    engine = ActionEngine(
        sources=sources, transforms=transforms, sinks=sinks, counters=counters, clock=clock
    )
    if store is not None and archive is not None and subscriptions_store is not None:
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
    if store is not None and archive is not None:
        # Scheduled analyses need the archive to read from; /action configures
        # them and this collector executes them, closing #41's Discord row.
        collectors.append(
            (
                ActionScheduler(
                    store=store,
                    engine=engine,
                    groups=group_policy,
                    clock=clock,
                    counters=counters,
                ),
                "action_tick",
            )
        )
    if store is not None:
        # Telegram drives the notifier through its bespoke Lifecycle hook;
        # here it is just another MessageCollector on the shared delivery
        # loop, because RepoNotifier was already collector-shaped.
        collectors.append(
            (RepoNotifier(store=store, groups=group_policy, engine=engine), "repo_notify")
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

    dm_sessions = SessionRegistry(
        pseudonyms=RandomPseudonymFactory(), clock=clock, ttl=config.session_ttl
    )
    transcription = DmTranscriptionService(
        publisher=publisher,
        sanitizer=sanitizer,
        sessions=dm_sessions,
        target_page=config.dm_target_base,
        edit_summary=config.edit_summary,
        debounce_seconds=config.burst_debounce.total_seconds(),
        timestamp_granularity=config.timestamp_granularity,
    )
    # Discord has no maintenance tick of its own, so expired DM sessions would
    # otherwise accumulate forever and degrade pseudonym minting.
    collectors.append((SessionSweeper(sessions=dm_sessions, counters=counters), "session_sweep"))
    commands = CommandService(
        directory=directory,
        groups=group_policy,
        page_url_for=config.page_url,
        counters=counters,
        capture_service=capture_service,
        vault=store,
        repo_actions=repo_gateway,
        actions=store if archive is not None else None,
        clock=clock,
        engine=engine,
        subscriptions=subscriptions_store,
        capabilities=DISCORD_CAPABILITIES,
        default_lang=config.llm_default_lang,
        repo_service=(
            GroupRepoService(gateway=repo_gateway, vault=store, directory=directory)
            if store
            else None
        ),
        repo_limiter=SlidingWindowLimiter(
            clock=clock, limit=config.log_throttle_per_minute, window=timedelta(minutes=1)
        ),
        llm_defaults=llm_defaults,
        llm_max_tokens_ceiling=config.llm_max_tokens_ceiling,
    )
    gateway = DiscordGateway(
        directory=directory,
        groups=group_policy,
        commands=commands,
        transcription=transcription,
        routes=DmRouteRegistry(clock=clock, route_ttl=config.session_ttl),
        capture=capture_service,
        masker=masker,
        subscriptions=subscriptions_store,
        analysis=analysis_service,
        default_lang=config.llm_default_lang,
    )

    async def release_clients() -> None:
        # Flush debounced DM lines before the publisher closes, or the last
        # burst of a private discussion is lost on shutdown (as Telegram's
        # Lifecycle already does).
        await transcription.flush_all()
        await publisher.aclose()
        await repo_gateway.aclose()
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
        bridge=bridge,
    )
    token = config.discord_bot_token

    def start() -> Coroutine[Any, Any, None]:
        return client.start(token)

    return PlatformRuntime(
        start=start,
        release=release_clients,
        # Unlike IRC, the client object exists before it connects, so the
        # router can be handed the transport up front.
        transport=DiscordTransport(client),
        platform="discord",
    )


def run_discord(config: Config) -> int:
    """Build the Discord object graph and start the gateway."""
    runtime = build_discord(config)
    asyncio.run(_run_alone(runtime))
    return 0


def build_irc(  # noqa: PLR0915 -- the root enumerates the object graph once
    config: Config, bridge: BridgeRouter | None = None
) -> PlatformRuntime:
    """Build the IRC object graph and run the session until the peer closes.

    The narrowest of the three roots, because IRC's capabilities admit the
    least: no durable DMs means no digest subscriptions, no message
    deletion means no requester-hiding, and no threads means every scope is
    a bare channel. What it does get is capture and the neutral analysis
    pipeline behind it — the reason the adapter is worth having.
    """
    counters = Counters()
    clock = SystemClock()
    group_policy = GroupPolicy(allowed=set(config.allowed_group_ids))

    store: ToolsDbStore | None = None
    archive: ToolsDbArchive | None = None
    if config.profile_encryption_key:
        runner = PymysqlRunner(
            host=config.toolsdb_host,
            database=config.toolsdb_name,
            cnf_path=Path(config.toolsdb_cnf),
        )
        store = ToolsDbStore(runner=runner, fernet_key=config.profile_encryption_key)
        if config.archive_pseudonym_key:
            archive = ToolsDbArchive(runner=runner)

    directory = ChannelDirectory(
        store=store,
        default_log_page=config.log_target_page,
        default_consent=config.consent_mode,
        default_repo="",
        page_suffix=config.wiki_page_suffix,
    )
    publisher = MetaWikiPublisher(
        api_url=config.wiki_api_url,
        username=config.wiki_username,
        botpassword=config.wiki_botpassword,
        user_agent=config.user_agent,
        max_attempts=config.wiki_max_retries,
        counters=counters,
    )
    sanitizer = WikitextSanitizer()
    repo_gateway = GitHubRepoGateway(user_agent=config.user_agent)
    llm_client: LiftWingClient | None = None
    capture_service: CaptureService | None = None
    masker: IrcAuthorMasker | None = None
    llm_defaults: LlmSettings | None = None
    collectors: list[tuple[MessageCollector, str]] = []

    # The same engine graph as the other roots, grown by deployment tier.
    sources: dict[str, Source] = {}
    transforms: dict[str, Transform] = {}
    sinks: dict[str, Sink] = {"chat_confirm": ChatConfirmSink()}
    if store is not None:
        sources["repo_events"] = RepoEventsSource(store=store, vault=store, gateway=repo_gateway)
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
        # No `subscriber_dm` sink here: durable_dm is False, so the
        # delivery gate refuses those modes before anything resolves it.
        sinks["reply"] = ChatReplySink(max_chars=IRC_CAPABILITIES.max_message_chars)
    engine = ActionEngine(
        sources=sources, transforms=transforms, sinks=sinks, counters=counters, clock=clock
    )

    if store is not None and archive is not None:
        capture_service = CaptureService(
            store=store,
            archive=archive,
            limiter=SlidingWindowLimiter(
                clock=clock, limit=config.capture_max_per_minute, window=timedelta(minutes=1)
            ),
            clock=clock,
            counters=counters,
            max_chars=IRC_CAPABILITIES.max_message_chars,
            retention_window=timedelta(days=config.capture_retention_days),
        )
        masker = IrcAuthorMasker(key=config.archive_pseudonym_key)
        collectors.append(
            (
                ActionScheduler(
                    store=store,
                    engine=engine,
                    groups=group_policy,
                    clock=clock,
                    counters=counters,
                ),
                "action_tick",
            )
        )
    if store is not None:
        collectors.append(
            (RepoNotifier(store=store, groups=group_policy, engine=engine), "repo_notify")
        )
    if capture_service is not None and store is not None and config.capture_reannounce_days:
        # Consent must not quietly age out as a channel's membership turns
        # over: the periodic re-announcement is the only thing that tells
        # people who joined after `capture on` that they are being archived.
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
    # No `sub_tick`: durable_dm=False gates digest subscriptions off, and no
    # `session_sweep`: IRC runs no DM transcription sessions to expire.

    commands = CommandService(
        directory=directory,
        groups=group_policy,
        page_url_for=config.page_url,
        counters=counters,
        capture_service=capture_service,
        vault=store,
        repo_actions=repo_gateway,
        actions=store if archive is not None else None,
        clock=clock,
        engine=engine,
        capabilities=IRC_CAPABILITIES,
        default_lang=config.llm_default_lang,
        repo_service=(
            GroupRepoService(gateway=repo_gateway, vault=store, directory=directory)
            if store
            else None
        ),
        repo_limiter=SlidingWindowLimiter(
            clock=clock, limit=config.log_throttle_per_minute, window=timedelta(minutes=1)
        ),
        llm_defaults=llm_defaults,
        llm_max_tokens_ceiling=config.llm_max_tokens_ceiling,
    )
    gateway = IrcGateway(
        directory=directory,
        groups=group_policy,
        commands=commands,
        clock=clock,
        nick=config.irc_nick,
        capture=capture_service,
        masker=masker,
        bridge=bridge,
    )

    async def release_clients() -> None:
        await publisher.aclose()
        await repo_gateway.aclose()
        if llm_client is not None:
            await llm_client.aclose()

    def start() -> Coroutine[Any, Any, None]:
        return _irc_main(
            config,
            gateway,
            store,
            archive,
            collectors=tuple(collectors),
            counters=counters,
            bridge=bridge,
        )

    return PlatformRuntime(start=start, release=release_clients, platform="irc")


def run_irc(config: Config) -> int:
    """Build the IRC object graph and run the session until the peer closes."""
    runtime = build_irc(config)
    asyncio.run(_run_alone(runtime))
    return 0


async def _run_alone(runtime: PlatformRuntime) -> None:
    """Run one platform, releasing its HTTP clients on the way out."""
    try:
        await runtime.start()
    finally:
        if runtime.release is not None:
            await runtime.release()


async def _irc_main(  # noqa: PLR0913 -- the root enumerates the object graph once
    config: Config,
    gateway: IrcGateway,
    store: ToolsDbStore | None,
    archive: ToolsDbArchive | None,
    *,
    collectors: tuple[tuple[MessageCollector, str], ...] = (),
    counters: Counters | None = None,
    bridge: BridgeRouter | None = None,
) -> None:
    """Bootstrap storage, dial the server, and run the session and its loops.

    The background collectors write to the *same* socket the session reads
    from, concurrently. That is safe because `IrcConnection.send_line` is
    the single writer and serializes under a lock — and because it paces
    every line through a token bucket, so a multi-line digest cannot flood
    the bot off the server.

    The session is the process: when the peer closes, the delivery loops
    are cancelled rather than left spinning against a dead socket.
    """
    if store is not None:
        try:
            await store.bootstrap()
            if archive is not None:
                await archive.bootstrap()
        except StorageError:
            log_event("storage_bootstrap", "error")
    reader, writer = await asyncio.open_connection(
        config.irc_server, config.irc_port, ssl=config.irc_tls or None
    )
    connection = IrcConnection(
        reader=reader,
        writer=writer,
        burst=config.irc_send_burst,
        min_interval=config.irc_send_interval,
    )
    session = IrcSession(
        channel=connection,
        gateway=gateway,
        nick=config.irc_nick,
        channels=config.irc_channels,
        password=config.irc_password,
    )
    transport = IrcTransport(channel=connection)
    if bridge is not None:
        # Only now can IRC be relayed *to*: the transport needs a live
        # socket, unlike Telegram's bot or the Discord client.
        bridge.register("irc", transport)
    poll_interval = config.events_poll_minutes * 60
    background = [
        asyncio.create_task(message_loop(transport, collector, poll_interval, label))
        for collector, label in collectors
    ]
    if counters is not None:
        background.append(
            asyncio.create_task(heartbeat_loop(counters, archive, _IRC_HEARTBEAT_SECONDS))
        )
    # Let every loop reach its first await before inbound traffic starts, so
    # the delivery side is armed rather than merely scheduled.
    await asyncio.sleep(0)
    try:
        await session.run()
    finally:
        for task in background:
            task.cancel()
        await asyncio.gather(*background, return_exceptions=True)
        await connection.close()


if __name__ == "__main__":
    raise SystemExit(main())
