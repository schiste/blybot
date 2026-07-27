"""Transport contract — every implementation proves these semantics.

The shared behaviour (``send`` delivers; ``capabilities.max_message_chars`` is
positive) runs against BOTH the real :class:`TelegramTransport` (driven by a
fake bot) and the in-repo :class:`FakeTransport`. The SDK-error → taxonomy
mapping is Telegram-specific, so it is asserted only against TelegramTransport.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import timedelta
from typing import Any, cast

import pytest
from telegram.error import RetryAfter, TelegramError, TimedOut

from blybot.adapters.telegram.transport import TelegramTransport
from blybot.domain.models import OutboundMessage, Scope
from blybot.domain.ports import (
    PermanentTransportError,
    RateLimited,
    TransientTransportError,
    Transport,
)
from tests.fakes import FakeTransport


class _RecordingBot:
    """A telegram Bot stand-in: records sends, or raises a scripted error."""

    def __init__(self, error: TelegramError | None = None) -> None:
        self._error = error
        self.sent: list[tuple[int, str, int | None]] = []

    async def send_message(
        self, chat_id: int, text: str, message_thread_id: int | None = None
    ) -> None:
        if self._error is not None:
            raise self._error
        self.sent.append((chat_id, text, message_thread_id))


# A transport case pairs the impl with a zero-arg "how many were delivered"
# probe, so one shared assertion works regardless of what each records.
TransportCase = tuple[Transport, Callable[[], int]]


def _telegram_case() -> TransportCase:
    bot = _RecordingBot()
    return TelegramTransport(cast("Any", bot)), lambda: len(bot.sent)


def _fake_case() -> TransportCase:
    transport = FakeTransport()
    return transport, lambda: len(transport.sent)


# One entry per implementation. Adding a future transport is a single line.
TRANSPORTS: list[tuple[str, Callable[[], TransportCase]]] = [
    ("telegram", _telegram_case),
    ("fake", _fake_case),
]


@pytest.fixture(params=[build for _, build in TRANSPORTS], ids=[name for name, _ in TRANSPORTS])
def case(request: pytest.FixtureRequest) -> TransportCase:
    build = request.param
    made: TransportCase = build()
    return made


async def test_send_delivers_the_message(case: TransportCase) -> None:
    transport, delivered = case
    await transport.send(OutboundMessage(scope=Scope("telegram", "-1"), text="hi"))
    assert delivered() == 1


def test_capabilities_report_a_positive_message_cap(case: TransportCase) -> None:
    transport, _delivered = case
    assert transport.capabilities.max_message_chars > 0


# --- Telegram-specific: the SDK-error → send-taxonomy mapping -----------------


def _telegram_raising(error: TelegramError) -> TelegramTransport:
    return TelegramTransport(cast("Any", _RecordingBot(error)))


async def test_retry_after_maps_to_rate_limited_preserving_the_wait() -> None:
    transport = _telegram_raising(RetryAfter(2))
    with pytest.raises(RateLimited) as excinfo:
        await transport.send(OutboundMessage(scope=Scope("telegram", "-1"), text="hi"))
    assert excinfo.value.retry_after == timedelta(seconds=2)


async def test_timeout_maps_to_a_transient_error() -> None:
    transport = _telegram_raising(TimedOut())
    with pytest.raises(TransientTransportError):
        await transport.send(OutboundMessage(scope=Scope("telegram", "-1"), text="hi"))


async def test_other_telegram_error_maps_to_a_permanent_error() -> None:
    transport = _telegram_raising(TelegramError("kicked"))
    with pytest.raises(PermanentTransportError):
        await transport.send(OutboundMessage(scope=Scope("telegram", "-1"), text="hi"))
