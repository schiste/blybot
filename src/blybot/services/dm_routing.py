"""Volatile routing state for private-message transcription.

Two classes, split along a line that matters (issue #45):

* :class:`DmRouteRegistry` is **the feature**: where a DM session currently
  publishes. Every platform needs it.
* :class:`PendingDmMessages` is a **workaround** for platforms where the bot
  cannot open a DM itself (``bot_can_open_dm=False``). There the user has to
  write to the bot first, and that message arrives with no indication of
  which channel prompted it — so it must be parked while the user picks a
  destination. Where the bot *can* open the DM, the flow starts in the target
  channel, the destination is known before any DM exists, and this class is
  never touched.

They used to be one class, which quietly baked one platform's two-phase
handshake into the neutral API any second platform would inherit.

Neither persists a user-to-channel association: routes and parked messages
live in process memory only, so a restart forgets both.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from blybot.domain.models import Scope
    from blybot.domain.ports import Clock

_MAX_REQUEST_ID = 2**31 - 1


@dataclass(frozen=True, slots=True)
class DmRoute:
    """Where one private DM session currently publishes."""

    scope: Scope
    page: str


@dataclass(frozen=True, slots=True)
class PendingDm:
    """One private message waiting for a destination selection."""

    text: str
    request_id: int
    opened_at: datetime


@dataclass
class DmRouteRegistry:
    """In-memory DM routes, keyed by DM scope. Needed on every platform."""

    clock: Clock
    route_ttl: timedelta
    _routes: dict[Scope, tuple[DmRoute, datetime]] = field(default_factory=dict)

    def save_route(self, dm: Scope, group: Scope, page: str) -> DmRoute:
        """Remember the destination for subsequent DMs from this scope."""
        self._prune()
        route = DmRoute(scope=group, page=page)
        self._routes[dm] = (route, self.clock.now())
        return route

    def route_for(self, dm: Scope) -> DmRoute | None:
        """Return the active route, expiring it with the session TTL."""
        entry = self._routes.get(dm)
        if entry is None:
            return None
        route, touched_at = entry
        if self.clock.now() - touched_at >= self.route_ttl:
            del self._routes[dm]
            return None
        return route

    def touch_route(self, dm: Scope) -> None:
        """Refresh a route after a DM is queued for it."""
        route = self.route_for(dm)
        if route is not None:
            self._routes[dm] = (route, self.clock.now())

    def _prune(self) -> None:
        now = self.clock.now()
        self._routes = {
            dm: (route, touched_at)
            for dm, (route, touched_at) in self._routes.items()
            if now - touched_at < self.route_ttl
        }


@dataclass
class PendingDmMessages:
    """Messages parked while their author chooses a destination.

    Only reached where ``bot_can_open_dm`` is False. The ``request_id`` pairs
    a parked message with the specific picker response that answers it, so a
    stale or replayed selection cannot publish the wrong text.
    """

    clock: Clock
    pending_ttl: timedelta = timedelta(minutes=5)
    _pending: dict[Scope, PendingDm] = field(default_factory=dict)
    _next_request_id: int = 1

    def open_pending(self, dm: Scope, text: str) -> int:
        """Park ``text`` and return the id the picker response must carry."""
        self._prune()
        request_id = self._next_request_id
        self._next_request_id = (
            self._next_request_id + 1 if self._next_request_id < _MAX_REQUEST_ID else 1
        )
        self._pending[dm] = PendingDm(text=text, request_id=request_id, opened_at=self.clock.now())
        return request_id

    def pop_pending(self, dm: Scope, request_id: int) -> str | None:
        """Consume a parked message if it matches this picker response."""
        pending = self._pending.get(dm)
        if pending is None:
            return None
        if self.clock.now() - pending.opened_at > self.pending_ttl:
            del self._pending[dm]
            return None
        if pending.request_id != request_id:
            return None
        del self._pending[dm]
        return pending.text

    def _prune(self) -> None:
        now = self.clock.now()
        self._pending = {
            dm: pending
            for dm, pending in self._pending.items()
            if now - pending.opened_at <= self.pending_ttl
        }
