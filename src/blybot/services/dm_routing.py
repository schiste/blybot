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
    from blybot.domain.ports import Clock

_MAX_REQUEST_ID = 2**31 - 1


@dataclass(frozen=True, slots=True)
class DmRoute:
    """Where one private DM session currently publishes."""

    group_chat_id: int
    thread_id: int
    page: str


@dataclass(frozen=True, slots=True)
class PendingDm:
    """One private message waiting for a destination selection."""

    text: str
    request_id: int
    opened_at: datetime


@dataclass
class DmRouteRegistry:
    """In-memory DM route and pending-message state."""

    clock: Clock
    route_ttl: timedelta
    pending_ttl: timedelta = timedelta(minutes=5)
    _routes: dict[int, tuple[DmRoute, datetime]] = field(default_factory=dict)
    _pending: dict[int, PendingDm] = field(default_factory=dict)
    _next_request_id: int = 1

    def open_pending(self, dm_chat_id: int, text: str) -> int:
        """Hold ``text`` until Telegram returns a chat picker result."""
        self._prune()
        request_id = self._next_request_id
        self._next_request_id = (
            self._next_request_id + 1 if self._next_request_id < _MAX_REQUEST_ID else 1
        )
        self._pending[dm_chat_id] = PendingDm(
            text=text, request_id=request_id, opened_at=self.clock.now()
        )
        return request_id

    def pop_pending(self, dm_chat_id: int, request_id: int) -> str | None:
        """Consume a pending message if it matches the picker response."""
        pending = self._pending.get(dm_chat_id)
        if pending is None:
            return None
        if self.clock.now() - pending.opened_at > self.pending_ttl:
            del self._pending[dm_chat_id]
            return None
        if pending.request_id != request_id:
            return None
        del self._pending[dm_chat_id]
        return pending.text

    def save_route(self, dm_chat_id: int, group_chat_id: int, thread_id: int, page: str) -> DmRoute:
        """Remember the selected destination for subsequent DMs."""
        route = DmRoute(group_chat_id=group_chat_id, thread_id=thread_id, page=page)
        self._routes[dm_chat_id] = (route, self.clock.now())
        return route

    def route_for(self, dm_chat_id: int) -> DmRoute | None:
        """Return the active route, expiring it with the session TTL."""
        entry = self._routes.get(dm_chat_id)
        if entry is None:
            return None
        route, touched_at = entry
        if self.clock.now() - touched_at >= self.route_ttl:
            del self._routes[dm_chat_id]
            return None
        return route

    def touch_route(self, dm_chat_id: int) -> None:
        """Refresh a route after a DM is queued for it."""
        route = self.route_for(dm_chat_id)
        if route is not None:
            self._routes[dm_chat_id] = (route, self.clock.now())

    def _prune(self) -> None:
        now = self.clock.now()
        self._pending = {
            chat_id: pending
            for chat_id, pending in self._pending.items()
            if now - pending.opened_at <= self.pending_ttl
        }
        self._routes = {
            chat_id: (route, touched_at)
            for chat_id, (route, touched_at) in self._routes.items()
            if now - touched_at < self.route_ttl
        }
