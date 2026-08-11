"""Per-platform relay queues that never drop, and say when they lag.

A busy Discord channel outruns IRC's flood pacing: IRC accepts roughly one
line every couple of seconds, and a single long message is several lines.
Something has to give, and the choice here is deliberate — **the mirror
stays complete**. Nothing is dropped and no message is silently lost, so
a channel reading the relay is reading everything.

What gives instead is latency, and latency is made *visible*: when a
platform's queue falls behind, the bot says so in the channels affected,
and says so again when it catches up. People can then self-moderate,
which a silently-truncated mirror never lets them do.

Two details that matter:

* The lag notice **bypasses the queue**. A "relay is running behind"
  message queued behind the backlog it describes would arrive after the
  problem was over.
* Each transition announces **once**, not per message. A backlog of two
  hundred lines must not produce two hundred warnings.

The ceiling is an OOM guard, not a drop policy: it sits far above the
announce threshold and exists so a pathological flood kills the backlog
rather than the process. Reaching it gets its own, louder notice.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from blybot.observability import log_event

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from blybot.domain.models import OutboundMessage, Scope

# Depth at which the relay is judged to be falling behind. Low enough that
# a human notices the warning before the lag is measured in minutes.
DEFAULT_DEGRADED_DEPTH = 20
# Far above it: only a pathological flood gets here, and the alternative
# to shedding is the process dying with the whole backlog anyway.
DEFAULT_CEILING = 5_000

NOTICE_DEGRADED = (
    "⏳ The relay to {platform} is running behind ({depth} messages queued); "
    "mirrored messages will arrive late until it catches up."
)
NOTICE_RECOVERED = "✅ The relay to {platform} has caught up."
NOTICE_OVERFLOWED = (
    "⚠️ The relay to {platform} is {dropped} messages over its limit and had to "
    "discard the oldest. The mirror is incomplete for that period."
)


@dataclass(eq=False)
class RelayQueue:
    """One platform's outbound backlog, drained in order by a worker."""

    platform: str
    send: Callable[[OutboundMessage], Awaitable[None]]
    notify: Callable[[list[Scope], str], Awaitable[None]]
    degraded_depth: int = DEFAULT_DEGRADED_DEPTH
    ceiling: int = DEFAULT_CEILING
    _pending: asyncio.Queue[OutboundMessage] = field(default_factory=asyncio.Queue, init=False)
    # Everyone with a stake in this platform's timeliness: the channels
    # relayed to it and the ones relaying into it. Bridges are small, so
    # this stays bounded without eviction.
    _audience: set[Scope] = field(default_factory=set, init=False)
    _degraded: bool = field(default=False, init=False)

    @property
    def depth(self) -> int:
        """How many messages are waiting."""
        return self._pending.qsize()

    async def submit(self, message: OutboundMessage, audience: list[Scope]) -> None:
        """Queue one message; announce if this is the moment we fell behind."""
        self._audience.update(audience)
        if self.depth >= self.ceiling:
            await self._overflow()
        self._pending.put_nowait(message)
        if not self._degraded and self.depth >= self.degraded_depth:
            self._degraded = True
            log_event("relay_queue", "ignored", reason="degraded")
            await self._announce(NOTICE_DEGRADED.format(platform=self.platform, depth=self.depth))

    async def drained(self) -> None:
        """Wait until everything queued so far has been sent."""
        await self._pending.join()

    async def run(self) -> None:
        """Drain the queue forever, one message at a time, in order."""
        while True:
            message = await self._pending.get()
            try:
                await self.send(message)
            finally:
                self._pending.task_done()
                await self._maybe_recovered()

    async def _maybe_recovered(self) -> None:
        """Announce catching up — once, when the backlog is actually gone."""
        if self._degraded and self._pending.empty():
            self._degraded = False
            log_event("relay_queue", "ok", reason="recovered")
            await self._announce(NOTICE_RECOVERED.format(platform=self.platform))

    async def _overflow(self) -> None:
        """Shed the oldest to stay alive, and say so — this is not routine.

        Sheds down to *half* the ceiling rather than by one. Shedding one
        at a time would put the queue back at the ceiling on the very next
        message, so every subsequent submit would announce again — turning
        one honest warning into a flood of its own.
        """
        dropped = 0
        while self.depth > self.ceiling // 2:
            self._pending.get_nowait()
            self._pending.task_done()
            dropped += 1
        log_event("relay_queue", "error", reason="overflow")
        await self._announce(NOTICE_OVERFLOWED.format(platform=self.platform, dropped=dropped))

    async def _announce(self, text: str) -> None:
        """Tell the audience, bypassing this queue so the notice is timely."""
        if self._audience:
            await self.notify(sorted(self._audience, key=lambda scope: scope.key), text)
