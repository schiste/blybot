"""Telegram transport wiring (spec section 8). Implementation lands in Phase 1.

Contract for the implementation (tracked in the Phase 1 milestone):

* long polling via ``python-telegram-bot`` (async), privacy mode stays ON;
* ``allowed_updates`` explicitly includes ``message``, ``my_chat_member``
  and ``chat_member``;
* handlers delegate immediately to services — no business logic here, and
  handlers extract *only* message text before crossing into the service
  layer (the anonymity boundary, spec R6);
* ``migrate_to_chat_id`` handled so supergroup upgrades don't silently
  break posting;
* operational logs carry event types and outcomes only — never message
  content or Telegram identifiers (spec section 16).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from blybot.config import Config
    from blybot.services.publish import LogPublicationService
    from blybot.services.sessions import SessionRegistry


def run_polling(
    config: Config,
    log_service: LogPublicationService,
    sessions: SessionRegistry,
) -> None:
    """Start the long-polling loop. Not yet implemented (Phase 1)."""
    raise NotImplementedError
