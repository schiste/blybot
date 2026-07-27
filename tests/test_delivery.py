"""Neutral delivery-loop tests: retry taxonomy, drop semantics, cadence.

The loop and its per-message retry state machine are platform-agnostic —
they reason only about the :class:`~blybot.domain.ports.Transport` port
and the abstract send taxonomy, never python-telegram-bot.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import timedelta
from typing import TYPE_CHECKING, Any, cast

import pytest

from blybot.domain.models import OutboundMessage, PlatformCapabilities, Scope
from blybot.domain.ports import RateLimited, TransientTransportError
from blybot.observability import Counters
from blybot.services.delivery import (
    _DELIVERY_MAX_RETRIES,
    _deliver,
    _next_deadline,
    message_loop,
)
from blybot.services.engine import ActionEngine
from blybot.services.notify import RepoNotifier
from blybot.services.policy import GroupPolicy
from blybot.services.schedule import ActionScheduler
from tests.fakes import FakeClock, FakeTransport, InMemoryActions, InMemoryProfiles

if TYPE_CHECKING:
    from collections.abc import Awaitable


def _empty_engine() -> ActionEngine:
    return ActionEngine(sources={}, transforms={}, sinks={}, counters=Counters(), clock=FakeClock())


async def _collect_sleeps() -> tuple[list[float], Any]:
    waits: list[float] = []

    async def sleep(seconds: float) -> None:
        waits.append(seconds)

    return waits, sleep


@dataclass
class _FlakyTransport:
    """Raises ``error`` for the first ``fail`` sends, then records success."""

    error: Exception
    fail: int
    capabilities: PlatformCapabilities = field(
        default_factory=lambda: PlatformCapabilities(max_message_chars=4096)
    )
    sent: list[OutboundMessage] = field(default_factory=list)

    async def send(self, message: OutboundMessage) -> None:
        if self.fail > 0:
            self.fail -= 1
            raise self.error
        self.sent.append(message)


async def test_deliver_waits_out_rate_limiting_then_sends() -> None:
    waits, sleep = await _collect_sleeps()
    transport = _FlakyTransport(RateLimited(timedelta(seconds=2)), fail=1)
    message = OutboundMessage(scope=Scope("telegram", "-1"), text="hi")
    await _deliver(cast("Any", transport), message, "repo_poll", sleep)
    assert transport.sent == [message]
    assert waits == [3.0]  # retry_after (2) + a 1s margin, then the retry lands


async def test_deliver_retries_a_transient_failure_then_sends() -> None:
    waits, sleep = await _collect_sleeps()
    transport = _FlakyTransport(TransientTransportError(), fail=1)
    message = OutboundMessage(scope=Scope("telegram", "-1", "7"), text="hi")
    await _deliver(cast("Any", transport), message, "action_tick", sleep)
    assert transport.sent == [message]
    assert waits == [1.0]


async def test_deliver_drops_after_persistent_rate_limiting() -> None:
    waits, sleep = await _collect_sleeps()
    transport = _FlakyTransport(RateLimited(timedelta(seconds=1)), fail=99)
    message = OutboundMessage(scope=Scope("telegram", "-1"), text="hi")
    await _deliver(cast("Any", transport), message, "repo_poll", sleep)
    assert transport.sent == []  # dropped, not retried forever
    assert len(waits) == _DELIVERY_MAX_RETRIES  # bounded retries, then give up


async def test_deliver_drops_after_persistent_transient_failure() -> None:
    waits, sleep = await _collect_sleeps()
    transport = _FlakyTransport(TransientTransportError(), fail=99)
    message = OutboundMessage(scope=Scope("telegram", "-1"), text="hi")
    await _deliver(cast("Any", transport), message, "action_tick", sleep)
    assert transport.sent == []
    assert len(waits) == _DELIVERY_MAX_RETRIES


async def test_deliver_drops_a_permanent_failure_at_once() -> None:
    waits, sleep = await _collect_sleeps()
    transport = FakeTransport(permanent_fail_keys={Scope("telegram", "-1").key})
    message = OutboundMessage(scope=Scope("telegram", "-1"), text="hi")
    await _deliver(cast("Any", transport), message, "repo_poll", sleep)
    assert transport.sent == []
    assert waits == []  # permanent errors are never retried


async def test_deliver_survives_a_non_taxonomy_error() -> None:
    """A bug in one send must not escape and kill the whole delivery task."""
    _waits, sleep = await _collect_sleeps()

    @dataclass
    class Boom:
        capabilities: PlatformCapabilities = field(
            default_factory=lambda: PlatformCapabilities(max_message_chars=4096)
        )

        async def send(self, message: OutboundMessage) -> None:
            del message
            msg = "schema drift"
            raise RuntimeError(msg)

    message = OutboundMessage(scope=Scope("telegram", "-1"), text="hi")
    # Must return (drop the message), not propagate.
    await _deliver(cast("Any", Boom()), message, "action_tick", sleep)


def test_next_deadline_holds_cadence_and_resyncs_after_overrun() -> None:
    # Work fit inside the interval: stay exactly on previous + interval.
    assert _next_deadline(previous=10.0, interval=5.0, now=12.0) == 15.0
    # Work overran the interval: resync to now + interval, no catch-up burst.
    assert _next_deadline(previous=10.0, interval=5.0, now=18.0) == 23.0


async def _run_loop_until_cancelled(coro: Awaitable[None], ticks: int = 20) -> None:
    task = asyncio.ensure_future(coro)
    for _ in range(ticks):
        await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


async def test_repo_notify_loop_delivers_and_survives_send_failures() -> None:
    notifier = RepoNotifier(
        store=InMemoryProfiles(), groups=GroupPolicy(allowed=set()), engine=_empty_engine()
    )
    good = OutboundMessage(scope=Scope("telegram", "-1", "7"), text="x/y:\n- Release")

    async def fake_collect() -> list[OutboundMessage]:
        return [OutboundMessage(scope=Scope("telegram", "-13"), text="lost"), good]

    notifier.collect = fake_collect  # type: ignore[method-assign]
    transport = FakeTransport(permanent_fail_keys={Scope("telegram", "-13").key})
    await _run_loop_until_cancelled(message_loop(transport, notifier, 0, "repo_poll"))
    # The zero-interval loop cycles repeatedly; the good scope delivers,
    # the -13 scope is permanently dropped and never lands.
    assert good in transport.sent
    assert all(m.scope.channel != "-13" for m in transport.sent)


async def test_action_tick_loop_delivers_and_survives_send_failures() -> None:
    scheduler = ActionScheduler(
        store=InMemoryActions(),
        engine=_empty_engine(),
        groups=GroupPolicy(allowed=set()),
        clock=FakeClock(),
        counters=Counters(),
    )
    good = OutboundMessage(scope=Scope("telegram", "-1", "7"), text="Published: url")

    async def fake_collect() -> list[OutboundMessage]:
        return [OutboundMessage(scope=Scope("telegram", "-13"), text="lost"), good]

    scheduler.collect = fake_collect  # type: ignore[method-assign]
    transport = FakeTransport(permanent_fail_keys={Scope("telegram", "-13").key})
    await _run_loop_until_cancelled(message_loop(transport, scheduler, 0, "action_tick"))
    assert good in transport.sent
    assert all(m.scope.channel != "-13" for m in transport.sent)


async def test_loop_survives_a_crashing_collect() -> None:
    """One bad poll cycle must never kill the loop for good."""
    notifier = RepoNotifier(
        store=InMemoryProfiles(), groups=GroupPolicy(allowed=set()), engine=_empty_engine()
    )
    calls = {"n": 0}

    async def exploding_collect() -> list[OutboundMessage]:
        calls["n"] += 1
        msg = "schema drift"
        raise RuntimeError(msg)

    notifier.collect = exploding_collect  # type: ignore[method-assign]
    await _run_loop_until_cancelled(
        message_loop(FakeTransport(), notifier, 0, "repo_poll"), ticks=30
    )
    assert calls["n"] >= 2  # it kept polling after the crash
