"""Composition root: builds the object graph and starts the transport.

This is the only module that knows about every layer. Run with
``python -m blybot`` or the ``blybot`` console script.
"""

from __future__ import annotations

import sys
from datetime import timedelta
from pathlib import Path

from blybot.adapters.github.gateway import GitHubRepoGateway
from blybot.adapters.github.issues import GitHubIssueTracker
from blybot.adapters.mediawiki.publisher import MetaWikiPublisher
from blybot.adapters.system import SystemClock
from blybot.adapters.telegram.admin import AdminHandlers
from blybot.adapters.telegram.app import Lifecycle, Maintenance, run_polling
from blybot.adapters.telegram.handlers import GroupHandlers, PrivateHandlers
from blybot.adapters.toolsdb.store import PymysqlRunner, ToolsDbStore
from blybot.config import ConfigurationError, load_config
from blybot.domain.pseudonym import RandomPseudonymFactory
from blybot.domain.sanitizer import WikitextSanitizer
from blybot.observability import Counters, configure_logging
from blybot.services.binding import TokenBinding
from blybot.services.directory import ChannelDirectory
from blybot.services.feedback import FeedbackService
from blybot.services.policy import GroupPolicy, SlidingWindowLimiter
from blybot.services.publish import LogPublicationService
from blybot.services.repo import GroupRepoService
from blybot.services.sessions import SessionRegistry
from blybot.services.transcribe import DmTranscriptionService


def main() -> int:
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
    store: ToolsDbStore | None = None
    if config.profile_encryption_key:
        try:
            store = ToolsDbStore(
                runner=PymysqlRunner(
                    host=config.toolsdb_host,
                    database=config.toolsdb_name,
                    cnf_path=Path(config.toolsdb_cnf),
                ),
                fernet_key=config.profile_encryption_key,
            )
        except ValueError:
            print(
                "configuration error: PROFILE_ENCRYPTION_KEY is not a valid Fernet key",
                file=sys.stderr,
            )
            return 2
    binding = TokenBinding(clock=clock)
    gateway = GitHubRepoGateway(user_agent=config.user_agent)

    async def release_clients() -> None:
        await publisher.aclose()
        await gateway.aclose()

    directory = ChannelDirectory(
        store=store,
        default_log_page=config.log_target_page,
        default_consent=config.consent_mode,
        default_repo=config.github_repo,
        page_prefix=config.wiki_page_prefix,
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
        groups=GroupPolicy(allowed=set(config.allowed_group_ids)),
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
    private_handlers = PrivateHandlers(
        transcription=transcription,
        sessions=sessions,
        counters=counters,
        welcome_text=config.welcome_text,
        dm_page_url=config.page_url(config.dm_target_base),
        maintainer=config.maintainer,
        issues_url=f"https://github.com/{config.github_repo}/issues",
        feedback=FeedbackService(tracker) if tracker else None,
        bug_limiter=SlidingWindowLimiter(
            clock=clock, limit=config.bug_throttle_per_hour, window=timedelta(hours=1)
        ),
        binding=binding,
        directory=directory,
        gateway=gateway,
        vault=store,
    )

    admin_handlers = AdminHandlers(
        directory=directory,
        counters=counters,
        page_url_for=config.page_url,
        binding=binding,
        vault=store,
    )

    run_polling(
        token=config.telegram_bot_token,
        group_handlers=group_handlers,
        private_handlers=private_handlers,
        admin_handlers=admin_handlers,
        lifecycle=Lifecycle(
            maintenance=Maintenance(sessions=sessions, counters=counters),
            transcription=transcription,
            release=release_clients,
            bootstrap=store.bootstrap if store else None,
        ),
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
