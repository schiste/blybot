"""TelegramTransport tests: SDK-error mapping and the capability descriptor."""

from __future__ import annotations

from datetime import timedelta
from typing import Any, cast

import pytest
from telegram.error import RetryAfter, TelegramError, TimedOut

from blybot.adapters.telegram.transport import TELEGRAM_CAPABILITIES, TelegramTransport
from blybot.domain.models import OutboundMessage, Scope
from blybot.domain.ports import (
    PermanentTransportError,
    RateLimited,
    TransientTransportError,
)


class _Bot:
    """Records sends, or raises a scripted error on send_message."""

    def __init__(self, error: TelegramError | None = None) -> None:
        self._error = error
        self.sent: list[tuple[int, str, int | None]] = []

    async def send_message(
        self, chat_id: int, text: str, message_thread_id: int | None = None
    ) -> None:
        if self._error is not None:
            raise self._error
        self.sent.append((chat_id, text, message_thread_id))


def _transport(error: TelegramError | None = None) -> tuple[TelegramTransport, _Bot]:
    bot = _Bot(error)
    return TelegramTransport(cast("Any", bot)), bot


async def test_send_routes_into_the_scope_topic() -> None:
    transport, bot = _transport()
    await transport.send(OutboundMessage(scope=Scope("telegram", "-1", "7"), text="hi"))
    assert bot.sent == [(-1, "hi", 7)]


async def test_retry_after_maps_to_rate_limited_preserving_the_wait() -> None:
    transport, _bot = _transport(RetryAfter(2))
    with pytest.raises(RateLimited) as excinfo:
        await transport.send(OutboundMessage(scope=Scope("telegram", "-1"), text="hi"))
    assert excinfo.value.retry_after == timedelta(seconds=2)


async def test_timeout_maps_to_a_transient_error() -> None:
    transport, _bot = _transport(TimedOut())
    with pytest.raises(TransientTransportError):
        await transport.send(OutboundMessage(scope=Scope("telegram", "-1"), text="hi"))


async def test_other_telegram_error_maps_to_a_permanent_error() -> None:
    transport, _bot = _transport(TelegramError("kicked"))
    with pytest.raises(PermanentTransportError):
        await transport.send(OutboundMessage(scope=Scope("telegram", "-1"), text="hi"))


def test_capabilities_reports_what_telegram_can_and_cannot_do() -> None:
    """Telegram is NOT strictly the most capable platform, which the old
    ``chat_picker`` flag accidentally implied: a Telegram bot cannot open a DM
    with a user who has not written to it first. That single limitation is why
    deep links and the destination picker exist at all."""
    transport, _bot = _transport()
    caps = transport.capabilities
    assert caps is TELEGRAM_CAPABILITIES
    assert caps.max_message_chars == 4096
    assert all(
        (
            caps.threads,
            caps.durable_dm,
            caps.deep_links,
            caps.message_delete,
            caps.id_can_change,
            caps.rich_choices,
        )
    )
    assert caps.bot_can_open_dm is False
