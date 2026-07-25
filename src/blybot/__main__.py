"""Composition root: builds the object graph and starts the transport.

This is the only module that knows about every layer. Run with
``python -m blybot`` or the ``blybot`` console script.
"""

from __future__ import annotations

import sys
from datetime import timedelta
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

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
from blybot.adapters.telegram.token_entry import TokenEntryHandler
from blybot.adapters.toolsdb.archive import ToolsDbArchive
from blybot.adapters.toolsdb.store import PymysqlRunner, ToolsDbStore
from blybot.config import ConfigurationError, load_config
from blybot.domain.models import LlmSettings
from blybot.domain.pseudonym import RandomPseudonymFactory
from blybot.domain.sanitizer import WikitextSanitizer
from blybot.observability import Counters, configure_logging
from blybot.services.analyze import (
    ArchiveWindowSource,
    PromptTransform,
    StatsTransform,
    TelegramReplySink,
    WikiSectionSink,
    explicit_page_resolver,
)
from blybot.services.binding import TokenBinding
from blybot.services.capture import CaptureReminder, CaptureService
from blybot.services.directory import ChannelDirectory
from blybot.services.dm_routing import DmRouteRegistry
from blybot.services.engine import ActionEngine
from blybot.services.feedback import FeedbackService
from blybot.services.notify import RepoNotifier
from blybot.services.policy import GroupPolicy, SlidingWindowLimiter
from blybot.services.publish import LogPublicationService
from blybot.services.repo import GroupRepoService
from blybot.services.schedule import ActionScheduler
from blybot.services.sessions import SessionRegistry
from blybot.services.transcribe import DmTranscriptionService


def main() -> int:  # noqa: PLR0915 -- the composition root enumerates the object graph once
    """Entry point."""
    try:
        config = load_config()
    except ConfigurationError as exc:
        print(f"configuration error: {exc}", file=sys.stderr)
        return 2

    configure_logging()
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
    capture_service: CaptureService | None = None
    capture_handlers: CaptureHandlers | None = None
    llm_client: LiftWingClient | None = None
    engine: ActionEngine | None = None
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
    binding = TokenBinding(clock=clock)
    gateway = GitHubRepoGateway(user_agent=config.user_agent)

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
    group_handlers = GroupHandlers(
        log_service=LogPublicationService(
            publisher=publisher,
            sanitizer=sanitizer,
            pseudonyms=pseudonyms,
            clock=clock,
            target_page=config.log_target_page,
            edit_summary=config.edit_summary,
            timestamp_granularity=config.timestamp_granularity,
        ),
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
        cleanup_delay_seconds=config.log_cleanup_seconds,
        reply_cleanup_delay_seconds=config.reply_cleanup_seconds,
    )
    tracker = (
        GitHubIssueTracker(
            repo=config.github_repo,
            token=config.github_token,
            user_agent=config.user_agent,
        )
        if config.github_token
        else None
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
        feedback=FeedbackService(tracker) if tracker else None,
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
        )
        capture_handlers = CaptureHandlers(
            service=capture_service,
            masker=HmacAuthorMasker(key=config.archive_pseudonym_key),
            directory=directory,
            groups=group_policy,
            bot_name=config.bot_name,
        )

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

        engine = ActionEngine(
            sources={"archive_window": ArchiveWindowSource(archive=archive)},
            transforms={
                "prompt": PromptTransform(
                    runners={"liftwing": llm_client},
                    store=store,
                    defaults=llm_defaults,
                    max_tokens_ceiling=config.llm_max_tokens_ceiling,
                    max_chunks=config.llm_max_chunks_per_run,
                    counters=counters,
                ),
                "stats": StatsTransform(),
            },
            sinks={
                "wiki_section": WikiSectionSink(
                    publisher=publisher,
                    sanitizer=sanitizer,
                    resolve_page=explicit_page_resolver(directory),
                    page_url_for=config.page_url,
                    edit_summary=config.edit_summary,
                    bot_name=config.bot_name,
                ),
                "telegram_reply": TelegramReplySink(),
            },
            counters=counters,
        )
        analysis_handlers = AnalysisHandlers(
            engine=engine,
            groups=group_policy,
            limiter=SlidingWindowLimiter(clock=clock, limit=6, window=timedelta(hours=1)),
            clock=clock,
            counters=counters,
        )

    admin_handlers = AdminHandlers(
        directory=directory,
        groups=group_policy,
        counters=counters,
        page_url_for=config.page_url,
        binding=binding,
        vault=store,
        archive=archive,
        capture_service=capture_service,
        llm_defaults=llm_defaults,
        llm_max_tokens_ceiling=config.llm_max_tokens_ceiling,
        actions=store if engine is not None else None,
        clock=clock,
    )

    notifier = (
        RepoNotifier(
            store=store, vault=store, gateway=gateway, groups=group_policy, counters=counters
        )
        if store
        else None
    )
    scheduler = (
        ActionScheduler(
            store=store, engine=engine, groups=group_policy, clock=clock, counters=counters
        )
        if store is not None and engine is not None
        else None
    )
    bootstrap: Callable[[], Awaitable[None]] | None = None
    if store is not None:
        profile_store, message_archive = store, archive

        async def bootstrap_storage() -> None:
            await profile_store.bootstrap()
            if message_archive is not None:
                await message_archive.bootstrap()

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
        maintenance=Maintenance(sessions=sessions, counters=counters, archive=archive),
        transcription=transcription,
        release=release_clients,
        bootstrap=bootstrap,
        notifier=notifier,
        scheduler=scheduler,
        reminder=reminder,
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
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
