"""Composition root: builds the object graph and starts the transport.

This is the only module that knows about every layer. Run with
``python -m blybot`` or the ``blybot`` console script.
"""

from __future__ import annotations

import sys
from datetime import timedelta

from blybot.adapters.mediawiki.publisher import MetaWikiPublisher
from blybot.adapters.system import SystemClock
from blybot.adapters.telegram.app import run_polling
from blybot.adapters.telegram.handlers import GroupHandlers, PrivateHandlers
from blybot.config import ConfigurationError, load_config
from blybot.domain.pseudonym import RandomPseudonymFactory
from blybot.domain.sanitizer import WikitextSanitizer
from blybot.observability import Counters, configure_logging
from blybot.services.policy import GroupPolicy, SlidingWindowLimiter
from blybot.services.publish import LogPublicationService
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
        counters=counters,
    )
    sessions = SessionRegistry(
        pseudonyms=RandomPseudonymFactory(),
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
    )
    group_handlers = GroupHandlers(
        log_service=LogPublicationService(
            publisher=publisher,
            sanitizer=sanitizer,
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
        consent_mode=config.consent_mode,
        counters=counters,
        group_greeting_text=config.group_greeting_text,
        log_page=config.log_target_page,
    )
    private_handlers = PrivateHandlers(
        transcription=transcription,
        sessions=sessions,
        counters=counters,
        welcome_text=config.welcome_text,
    )

    run_polling(
        token=config.telegram_bot_token,
        group_handlers=group_handlers,
        private_handlers=private_handlers,
        sessions=sessions,
        transcription=transcription,
        counters=counters,
        shutdown=publisher.aclose,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
