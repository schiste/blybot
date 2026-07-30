"""In-memory DM session registry (spec R4, section 10).

The registry is the *only* place a private chat is held (as a DM
:class:`Scope`, whose ``channel`` is the private chat id), and it is held
exclusively in process memory — never serialized, never logged. Sessions
vanish on TTL expiry, on explicit reset, or on process restart; all three
are acceptable by design.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import timedelta
from typing import TYPE_CHECKING, Final

from blybot.domain.models import Session
from blybot.observability import log_event

if TYPE_CHECKING:
    from blybot.domain.models import OutboundMessage, Scope
    from blybot.domain.ports import Clock, PseudonymFactory
    from blybot.observability import Counters

_MINT_ATTEMPTS: Final = 32


@dataclass
class SessionRegistry:
    """Volatile map of DM :class:`Scope` -> :class:`Session`."""

    pseudonyms: PseudonymFactory
    clock: Clock
    ttl: timedelta
    _sessions: dict[Scope, Session] = field(default_factory=dict)

    def touch(self, scope: Scope) -> Session:
        """Return the live session for ``scope``, minting one if needed.

        Accessing a session refreshes its ``last_seen``; an expired
        session is replaced by a fresh identity rather than revived.
        """
        live = self.peek(scope)
        if live is None:
            return self._mint(scope)
        return self._store(scope, replace(live, last_seen=self.clock.now()))

    def advance(self, scope: Scope) -> Session:
        """Like :meth:`touch`, but also count one recorded message.

        The returned session's ``message_count`` is the 1-based ordinal
        of the message being recorded — its discussion indentation depth.
        """
        current = self.touch(scope)
        return self._store(scope, replace(current, message_count=current.message_count + 1))

    def peek(self, scope: Scope) -> Session | None:
        """Return the live session without refreshing or minting one.

        Expired sessions read as ``None``: an aged-out identity is never
        revived, only replaced by :meth:`touch`.
        """
        existing = self._sessions.get(scope)
        if existing is None or self.clock.now() - existing.last_seen >= self.ttl:
            return None
        return existing

    def reset(self, scope: Scope) -> Session:
        """Force a new identity for ``scope`` (explicit ``/start``, spec section 10)."""
        return self._mint(scope)

    def sweep(self) -> int:
        """Drop all expired sessions; return how many were removed."""
        now = self.clock.now()
        expired = [key for key, s in self._sessions.items() if now - s.last_seen >= self.ttl]
        for key in expired:
            del self._sessions[key]
        return len(expired)

    def _mint(self, scope: Scope) -> Session:
        # Re-draw when a pseudonym is already held by any known session
        # (live or its own predecessor): two concurrent discussions must
        # never share a section heading, and a reset must actually
        # change identity. Bounded so a tiny factory namespace degrades
        # to a repeat rather than an infinite loop.
        taken = {session.anchor for session in self._sessions.values()}
        pseudonym = self.pseudonyms.mint()
        for _ in range(_MINT_ATTEMPTS - 1):
            if pseudonym.value not in taken:
                break
            pseudonym = self.pseudonyms.mint()
        now = self.clock.now()
        session = Session(
            pseudonym=pseudonym,
            anchor=pseudonym.value,
            last_seen=now,
            created_at=now,
        )
        return self._store(scope, session)

    def _store(self, scope: Scope, session: Session) -> Session:
        self._sessions[scope] = session
        return session


@dataclass(eq=False)
class SessionSweeper:
    """A :class:`~blybot.services.delivery.MessageCollector` that only evicts.

    :meth:`SessionRegistry.peek` already expires a session logically, so an
    unswept registry keeps *behaving* correctly — but the dict only ever
    grows, and ``_mint`` draws its collision-avoidance set from every entry
    it holds, live or dead. Left unswept long enough, minting degrades toward
    reusing a pseudonym, and two concurrent discussions could share a section
    heading — the one thing ``_mint`` exists to prevent.

    Telegram sweeps from its maintenance tick. This exists so a platform with
    no tick of its own (Discord) gets the same eviction through the shared
    delivery loop instead of leaking. Returns no messages, ever.
    """

    sessions: SessionRegistry
    counters: Counters

    async def collect(self) -> list[OutboundMessage]:
        """Drop expired sessions, reporting the count exactly as Telegram does."""
        expired = self.sessions.sweep()
        if expired:
            self.counters.increment("sessions_expired", expired)
            log_event("session_sweep", "ok", expired=expired)
        return []
