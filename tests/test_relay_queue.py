"""The relay backlog: never drops, and says when it lags (#80)."""

from __future__ import annotations

import asyncio

from blybot.domain.models import OutboundMessage, Scope
from blybot.services.relay_queue import RelayQueue

TG = Scope("telegram", "-100500")
IRC = Scope("irc", "#wikipedia-fr")


class _Harness:
    """A queue whose sends and notices are recorded rather than delivered."""

    def __init__(self, *, degraded_depth: int = 3, ceiling: int = 10) -> None:
        self.sent: list[str] = []
        self.notices: list[tuple[list[Scope], str]] = []
        self.block = asyncio.Event()
        self.block.set()
        self.queue = RelayQueue(
            platform="irc",
            send=self._send,
            notify=self._notify,
            degraded_depth=degraded_depth,
            ceiling=ceiling,
        )

    async def _send(self, message: OutboundMessage) -> None:
        await self.block.wait()  # stand in for IRC's flood pacing
        self.sent.append(message.text)

    async def _notify(self, scopes: list[Scope], text: str) -> None:
        self.notices.append((scopes, text))

    async def submit(self, *texts: str) -> None:
        for text in texts:
            await self.queue.submit(OutboundMessage(scope=IRC, text=text), [TG, IRC])

    async def drain(self) -> None:
        worker = asyncio.ensure_future(self.queue.run())
        await self.queue.drained()
        worker.cancel()
        await asyncio.gather(worker, return_exceptions=True)


async def test_a_backlog_is_delivered_in_full_and_in_order() -> None:
    """The mirror stays complete; latency is what gives, not content."""
    harness = _Harness()
    await harness.submit(*[f"line {index}" for index in range(8)])

    await harness.drain()

    assert harness.sent == [f"line {index}" for index in range(8)]


async def test_falling_behind_is_announced_once_not_per_message() -> None:
    """A backlog of two hundred lines must not produce two hundred warnings."""
    harness = _Harness(degraded_depth=3)
    await harness.submit("a", "b")
    assert harness.notices == []  # still keeping up

    await harness.submit("c", "d", "e", "f")

    assert len(harness.notices) == 1
    (scopes, text) = harness.notices[0]
    assert "running behind" in text
    assert scopes == [IRC, TG]  # everyone with a stake, sorted


async def test_catching_up_is_announced_when_the_backlog_is_actually_gone() -> None:
    harness = _Harness(degraded_depth=3)
    await harness.submit("a", "b", "c", "d")
    assert len(harness.notices) == 1

    await harness.drain()

    assert len(harness.notices) == 2
    assert "caught up" in harness.notices[1][1]


async def test_a_queue_that_never_fell_behind_never_announces() -> None:
    harness = _Harness(degraded_depth=100)
    await harness.submit("a", "b")
    await harness.drain()
    assert harness.notices == []


async def test_the_ceiling_sheds_the_oldest_and_says_so_loudly() -> None:
    """An OOM guard, not a drop policy — so it must never look routine."""
    harness = _Harness(degraded_depth=100, ceiling=4)
    await harness.submit("a", "b", "c", "d")  # at the ceiling
    await harness.submit("e")  # one too many

    # Shed down to half, not by one: shedding one would put the queue back
    # at the ceiling on the next message and announce all over again.
    assert harness.queue.depth == 3
    overflow = [text for _scopes, text in harness.notices if "discard" in text]
    assert len(overflow) == 1
    assert "incomplete" in overflow[0]  # honest about what was lost

    await harness.drain()
    assert harness.sent == ["c", "d", "e"]  # the oldest went, the newest stayed


async def test_a_sustained_overflow_announces_per_shed_not_per_message() -> None:
    """Shedding to half is what bounds the warnings.

    Each overflow buys `ceiling // 2` more messages before the next one, so
    the notice rate is a fraction of the message rate rather than tracking
    it. In production (ceiling 5000) one shed buys 2500 messages.
    """
    ceiling, messages = 20, 60
    harness = _Harness(degraded_depth=1000, ceiling=ceiling)
    await harness.submit(*[str(index) for index in range(messages)])

    overflow = [text for _scopes, text in harness.notices if "discard" in text]
    naive = messages - ceiling  # one warning per message past the cap
    assert len(overflow) <= messages // (ceiling // 2)
    assert len(overflow) < naive // 4  # an order of magnitude quieter


async def test_a_send_failure_still_advances_the_queue() -> None:
    """One undeliverable message must not wedge every later one behind it."""
    sent: list[str] = []

    async def send(message: OutboundMessage) -> None:
        if message.text == "bad":
            msg = "transport refused"
            raise RuntimeError(msg)
        sent.append(message.text)

    async def notify(_scopes: list[Scope], _text: str) -> None:
        return None

    queue = RelayQueue(platform="irc", send=send, notify=notify)
    for text in ("good", "bad", "later"):
        await queue.submit(OutboundMessage(scope=IRC, text=text), [IRC])

    worker = asyncio.ensure_future(queue.run())
    for _ in range(10):  # let the worker chew through what it can
        await asyncio.sleep(0)
    worker.cancel()
    await asyncio.gather(worker, return_exceptions=True)

    assert sent == ["good"]  # "bad" raised out of the worker...
    assert queue.depth == 1  # ...and "later" is still queued, not lost


async def test_a_notice_with_nobody_to_tell_is_simply_skipped() -> None:
    """Nothing has been relayed yet, so no channel has a stake in this."""
    notices: list[str] = []

    async def notify(_scopes: list[Scope], text: str) -> None:
        notices.append(text)

    queue = RelayQueue(
        platform="irc",
        send=lambda _message: asyncio.sleep(0),
        notify=notify,
        degraded_depth=1,
    )
    await queue.submit(OutboundMessage(scope=IRC, text="x"), [])
    assert notices == []
