"""Volatile routing state for private-message transcription.

Private Telegram updates do not say which group prompted the user to
write to the bot. This registry fills that gap without persisting a
user-to-group association: it holds one pending message while the user
chooses a group, then remembers the selected route only in process
memory.
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
    """In-memory DM route and pending-message state, keyed by DM scope."""

    clock: Clock
    route_ttl: timedelta
    pending_ttl: timedelta = timedelta(minutes=5)
    _routes: dict[Scope, tuple[DmRoute, datetime]] = field(default_factory=dict)
    _pending: dict[Scope, PendingDm] = field(default_factory=dict)
    _next_request_id: int = 1

    def open_pending(self, dm: Scope, text: str) -> int:
        """Hold ``text`` until Telegram returns a chat picker result."""
        self._prune()
        request_id = self._next_request_id
        self._next_request_id = (
            self._next_request_id + 1 if self._next_request_id < _MAX_REQUEST_ID else 1
        )
        self._pending[dm] = PendingDm(text=text, request_id=request_id, opened_at=self.clock.now())
        return request_id

    def pop_pending(self, dm: Scope, request_id: int) -> str | None:
        """Consume a pending message if it matches the picker response."""
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

    def save_route(self, dm: Scope, group: Scope, page: str) -> DmRoute:
        """Remember the selected destination for subsequent DMs."""
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
        self._pending = {
            dm: pending
            for dm, pending in self._pending.items()
            if now - pending.opened_at <= self.pending_ttl
        }
        self._routes = {
            dm: (route, touched_at)
            for dm, (route, touched_at) in self._routes.items()
            if now - touched_at < self.route_ttl
        }
