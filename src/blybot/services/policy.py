"""Group admission policy and abuse throttling (spec 8, 15; N4).

Both are pure in-memory policy: the throttle keys hold chat/user ids
transiently (never persisted, never logged), consistent with spec R6.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from datetime import datetime, timedelta

    from blybot.domain.ports import Clock


@dataclass
class GroupPolicy:
    """Decides which group chats the bot serves.

    An empty allowlist means every group the operator adds the bot to is
    served (spec 12 default); a non-empty list restricts service to
    exactly those chat ids. Supergroup migration (spec 8) rewrites the
    in-memory reference so an upgraded group keeps working.
    """

    allowed: set[int]

    def is_allowed(self, chat_id: int) -> bool:
        """Return whether the bot should serve this group chat."""
        return not self.allowed or chat_id in self.allowed

    def migrate(self, old_chat_id: int, new_chat_id: int) -> bool:
        """Rewrite ``old_chat_id`` to ``new_chat_id``; return whether it applied."""
        if old_chat_id in self.allowed:
            self.allowed.discard(old_chat_id)
            self.allowed.add(new_chat_id)
            return True
        return False


@dataclass
class SlidingWindowLimiter:
    """Per-key sliding-window rate limiter for ``/log`` flooding (N4)."""

    clock: Clock
    limit: int
    window: timedelta
    _events: dict[tuple[str, int], deque[datetime]] = field(default_factory=dict)
    _next_evict: datetime | None = field(default=None, init=False)

    def allow(self, scope: str, key: int) -> bool:
        """Record one event for ``(scope, key)``; return whether it is within the cap."""
        now = self.clock.now()
        self._evict_expired(now)
        history = self._events.setdefault((scope, key), deque())
        cutoff = now - self.window
        while history and history[0] <= cutoff:
            history.popleft()
        if len(history) >= self.limit:
            return False
        history.append(now)
        return True

    def _evict_expired(self, now: datetime) -> None:
        """Drop keys whose events have all expired (amortized once per window).

        Without this the per-``(scope, key)`` map grows forever — one entry
        per distinct user (``/log``, ``/bug``) or scope ever seen — since
        ``allow`` only ever prunes the key it touches.
        """
        if self._next_evict is not None and now < self._next_evict:
            return
        self._next_evict = now + self.window
        cutoff = now - self.window
        stale = [key for key, history in self._events.items() if history[-1] <= cutoff]
        for key in stale:
            del self._events[key]
