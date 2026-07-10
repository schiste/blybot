"""Composition root: builds the object graph and starts the transport.

This is the only module that knows about every layer. Run with
``python -m blybot`` or the ``blybot`` console script.
"""

from __future__ import annotations

import sys

from blybot.adapters.mediawiki.publisher import MetaWikiPublisher
from blybot.adapters.system import SystemClock
from blybot.adapters.telegram.app import run_polling
from blybot.config import ConfigurationError, load_config
from blybot.domain.pseudonym import RandomPseudonymFactory
from blybot.domain.sanitizer import WikitextSanitizer
from blybot.services.publish import LogPublicationService
from blybot.services.sessions import SessionRegistry


def main() -> int:
    """Entry point."""
    try:
        config = load_config()
    except ConfigurationError as exc:
        print(f"configuration error: {exc}", file=sys.stderr)
        return 2

    clock = SystemClock()
    publisher = MetaWikiPublisher(
        api_url=config.wiki_api_url,
        username=config.wiki_username,
        botpassword=config.wiki_botpassword,
        user_agent=config.user_agent,
    )
    log_service = LogPublicationService(
        publisher=publisher,
        sanitizer=WikitextSanitizer(),
        clock=clock,
        target_page=config.log_target_page,
        edit_summary=config.edit_summary,
        timestamp_granularity=config.timestamp_granularity,
    )
    sessions = SessionRegistry(
        pseudonyms=RandomPseudonymFactory(),
        clock=clock,
        ttl=config.session_ttl,
    )

    run_polling(config, log_service, sessions)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
