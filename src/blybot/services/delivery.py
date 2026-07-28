"""Platform-agnostic outbound delivery: the retry/drop loop behind a Transport.

Every background collector (repo digests, scheduled actions, capture
reminders, DM subscriptions) hands its cycle's messages to :func:`message_loop`,
which delivers each through a :class:`~blybot.domain.ports.Transport` and
reasons only about the abstract send taxonomy — never a platform's SDK
errors. Importing nothing but domain + ports, the same loop drives every
platform's transport, so a second adapter reuses it unchanged.
"""

from __future__ import annotations

import asyncio
import time
from typing import TYPE_CHECKING, Final, Protocol

from blybot.domain.ports import (
    PermanentTransportError,
    RateLimited,
    TransientTransportError,
)
from blybot.observability import log_event

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from blybot.domain.models import OutboundMessage
    from blybot.domain.ports import Transport


class MessageCollector(Protocol):
    """Anything the background tick can poll for outbound chat messages.

    The repo notifier, action scheduler, capture reminder and subscription
    scheduler all implement this — one delivery loop serves every
    collector (v3 phase 5 unification).
    """

    async def collect(self) -> list[OutboundMessage]:
        """Return this cycle's messages; called once per tick."""
        ...


_DELIVERY_MAX_RETRIES: Final = 3


async def _deliver(
    transport: Transport,
    message: OutboundMessage,
    label: str,
    sleep: Callable[[float], Awaitable[None]],
) -> None:
    """Send one message through ``transport``, honoring the send taxonomy.

    :class:`RateLimited` (the bot is throttled) is waited out and retried;
    :class:`TransientTransportError` (a network blip) is retried after a
    short pause. Both are bounded by ``_DELIVERY_MAX_RETRIES`` — a scope
    that stays throttled past the budget is dropped like any other
    undeliverable message, never retried forever. A
    :class:`PermanentTransportError` (kicked, muted, chat-not-found) is
    permanent for this message and dropped at once. Any other exception is
    a bug in the transport — drop this one message and keep the delivery
    task alive, mirroring the collect() guard (log its type only, never
    its text).
    """
    reason = ""
    delay = 0.0
    for attempt in range(_DELIVERY_MAX_RETRIES + 1):
        try:
            await transport.send(message)
        except RateLimited as exc:
            # Honor the stated wait plus a 1s margin.
            reason, delay = "flood", exc.retry_after.total_seconds() + 1
        except TransientTransportError:
            reason, delay = "timeout", 1.0
        except PermanentTransportError:
            # Kicked from the group, muted, etc. — that scope's message is
            # lost, every other scope's still goes out.
            log_event(f"{label}_delivery", "ignored")
            return
        except Exception as exc:  # keep the delivery task alive
            log_event(f"{label}_delivery", "error", error=type(exc).__name__)
            return
        else:
            return  # sent
        if attempt < _DELIVERY_MAX_RETRIES:
            await sleep(delay)
    # Still rate-limited/timing-out past the retry budget: drop like any
    # other undeliverable message rather than retry forever.
    log_event(f"{label}_delivery", "ignored", reason=reason)


def _next_deadline(previous: float, interval: float, now: float) -> float:
    """The next tick deadline: anchored to the schedule, missed ticks not replayed.

    When the work finished within the interval the cadence stays exactly
    on ``previous + interval`` (no drift); when it overran, the deadline
    resyncs to ``now + interval`` so a slow tick does not trigger a burst
    of catch-up ticks.
    """
    candidate = previous + interval
    return candidate if candidate > now else now + interval


async def message_loop(  # noqa: PLR0913 -- sleep/monotonic are injected test seams
    transport: Transport,
    collector: MessageCollector,
    interval_seconds: float,
    label: str,
    *,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    monotonic: Callable[[], float] = time.monotonic,
) -> None:
    """Poll ``collector`` and deliver its messages until cancelled.

    The cadence is anchored to a fixed deadline rather than sleeping a
    whole interval *after* the work, so a slow tick (e.g. a minutes-long
    analysis) does not push every later tick progressively late.
    """
    next_at = monotonic() + interval_seconds
    while True:
        await sleep(max(0.0, next_at - monotonic()))
        try:
            messages = await collector.collect()
        except Exception:
            log_event(label, "error")
            messages = []
        for message in messages:
            await _deliver(transport, message, label, sleep)
        next_at = _next_deadline(next_at, interval_seconds, monotonic())
