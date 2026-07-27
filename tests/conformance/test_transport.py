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

import discord
import pytest
from telegram.error import RetryAfter, TelegramError, TimedOut

from blybot.adapters.discord.transport import DiscordTransport
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


class _FakeResponse:
    """Minimal aiohttp-response stand-in for building discord HTTP errors."""

    def __init__(self, status: int) -> None:
        self.status = status
        self.reason = "test"


class _RecordingChannel:
    """A discord ``Messageable`` stand-in: records sends, or raises a script."""

    def __init__(self, error: Exception | None = None) -> None:
        self._error = error
        self.sent: list[str] = []

    async def send(self, content: str) -> None:
        if self._error is not None:
            raise self._error
        self.sent.append(content)


class _FakeDiscordClient:
    """A discord.Client stand-in resolving one channel by cache or fetch."""

    def __init__(self, channel: _RecordingChannel, *, cached: bool = True) -> None:
        self._channel = channel
        self._cached = cached
        self.fetched: list[int] = []

    def get_channel(self, channel_id: int) -> _RecordingChannel | None:
        del channel_id
        return self._channel if self._cached else None

    async def fetch_channel(self, channel_id: int) -> _RecordingChannel:
        self.fetched.append(channel_id)
        return self._channel


def _discord_transport(channel: _RecordingChannel, *, cached: bool = True) -> DiscordTransport:
    client = _FakeDiscordClient(channel, cached=cached)
    return DiscordTransport(cast("Any", client))


def _discord_case() -> TransportCase:
    channel = _RecordingChannel()
    return _discord_transport(channel), lambda: len(channel.sent)


# One entry per implementation. Adding a future transport is a single line.
TRANSPORTS: list[tuple[str, Callable[[], TransportCase]]] = [
    ("telegram", _telegram_case),
    ("discord", _discord_case),
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


# --- Discord-specific: channel resolution, chunking, and the error mapping ----

_DISCORD_SCOPE = Scope("discord", "555000111")


async def test_discord_resolves_via_rest_fetch_when_not_cached() -> None:
    channel = _RecordingChannel()
    transport = _discord_transport(channel, cached=False)
    client = cast("_FakeDiscordClient", transport.client)
    await transport.send(OutboundMessage(scope=_DISCORD_SCOPE, text="hi"))
    assert channel.sent == ["hi"]
    assert client.fetched == [555000111]  # cache miss fell back to fetch_channel


async def test_discord_sends_a_thread_scope_to_the_thread_channel() -> None:
    channel = _RecordingChannel()
    transport = _discord_transport(channel)
    client = cast("_FakeDiscordClient", transport.client)
    scope = Scope("discord", "555000111", "999888777")
    await transport.send(OutboundMessage(scope=scope, text="hi"))
    # The thread id (not the parent channel) is the sendable target.
    assert client.get_channel(0) is channel
    assert channel.sent == ["hi"]


async def test_discord_chunks_text_at_the_2000_char_cap() -> None:
    channel = _RecordingChannel()
    transport = _discord_transport(channel)
    text = "a" * 2500
    await transport.send(OutboundMessage(scope=_DISCORD_SCOPE, text=text))
    assert [len(chunk) for chunk in channel.sent] == [2000, 500]


async def test_discord_rate_limited_maps_preserving_the_wait() -> None:
    channel = _RecordingChannel(discord.RateLimited(2.5))
    transport = _discord_transport(channel)
    with pytest.raises(RateLimited) as excinfo:
        await transport.send(OutboundMessage(scope=_DISCORD_SCOPE, text="hi"))
    assert excinfo.value.retry_after == timedelta(seconds=2.5)


async def test_discord_leaked_http_429_maps_to_rate_limited() -> None:
    channel = _RecordingChannel(discord.HTTPException(cast("Any", _FakeResponse(429)), "slow"))
    transport = _discord_transport(channel)
    with pytest.raises(RateLimited) as excinfo:
        await transport.send(OutboundMessage(scope=_DISCORD_SCOPE, text="hi"))
    assert excinfo.value.retry_after == timedelta(0)


async def test_discord_server_error_maps_to_a_transient_error() -> None:
    channel = _RecordingChannel(discord.DiscordServerError(cast("Any", _FakeResponse(503)), "5xx"))
    transport = _discord_transport(channel)
    with pytest.raises(TransientTransportError):
        await transport.send(OutboundMessage(scope=_DISCORD_SCOPE, text="hi"))


async def test_discord_timeout_maps_to_a_transient_error() -> None:
    channel = _RecordingChannel(TimeoutError())
    transport = _discord_transport(channel)
    with pytest.raises(TransientTransportError):
        await transport.send(OutboundMessage(scope=_DISCORD_SCOPE, text="hi"))


async def test_discord_forbidden_maps_to_a_permanent_error() -> None:
    channel = _RecordingChannel(discord.Forbidden(cast("Any", _FakeResponse(403)), "no access"))
    transport = _discord_transport(channel)
    with pytest.raises(PermanentTransportError):
        await transport.send(OutboundMessage(scope=_DISCORD_SCOPE, text="hi"))
