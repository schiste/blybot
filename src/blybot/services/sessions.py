"""In-memory DM session registry (spec R4, section 10).

The registry is the *only* place a Telegram private ``chat_id`` is held,
and it is held exclusively in process memory — never serialized, never
logged. Sessions vanish on TTL expiry, on explicit reset, or on process
restart; all three are acceptable by design.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta
from typing import TYPE_CHECKING

from blybot.domain.models import Session

if TYPE_CHECKING:
    from blybot.domain.ports import Clock, PseudonymFactory


@dataclass
class SessionRegistry:
    """Volatile map of private chat id -> :class:`Session`."""

    pseudonyms: PseudonymFactory
    clock: Clock
    ttl: timedelta
    _sessions: dict[int, Session] = field(default_factory=dict)

    def touch(self, chat_id: int) -> Session:
        """Return the live session for ``chat_id``, minting one if needed.

        Accessing a session refreshes its ``last_seen``; an expired
        session is replaced by a fresh identity rather than revived.
        """
        now = self.clock.now()
        existing = self._sessions.get(chat_id)
        if existing is not None and now - existing.last_seen < self.ttl:
            refreshed = Session(
                pseudonym=existing.pseudonym,
                anchor=existing.anchor,
                last_seen=now,
            )
            self._sessions[chat_id] = refreshed
            return refreshed
        return self._mint(chat_id)

    def reset(self, chat_id: int) -> Session:
        """Force a new identity for ``chat_id`` (explicit ``/start``, spec section 10)."""
        return self._mint(chat_id)

    def sweep(self) -> int:
        """Drop all expired sessions; return how many were removed."""
        now = self.clock.now()
        expired = [key for key, s in self._sessions.items() if now - s.last_seen >= self.ttl]
        for key in expired:
            del self._sessions[key]
        return len(expired)

    def _mint(self, chat_id: int) -> Session:
        pseudonym = self.pseudonyms.mint()
        session = Session(
            pseudonym=pseudonym,
            anchor=pseudonym.value,
            last_seen=self.clock.now(),
        )
        self._sessions[chat_id] = session
        return session
