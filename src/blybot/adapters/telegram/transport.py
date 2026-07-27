"""The Telegram :class:`~blybot.domain.ports.Transport` implementation.

This is the outbound edge of the Telegram adapter: it converts a neutral
:class:`~blybot.domain.models.OutboundMessage` back into a Telegram send
(via the existing :func:`telegram_target` / :func:`send_threaded` helpers)
and maps python-telegram-bot's SDK errors onto the platform-agnostic send
taxonomy the delivery loop reasons about. It also publishes Telegram's
:class:`~blybot.domain.models.PlatformCapabilities` so features can gate
without importing anything Telegram-specific.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from typing import TYPE_CHECKING, Final

from telegram.error import RetryAfter, TelegramError, TimedOut

from blybot.adapters.telegram._common import send_threaded, telegram_target
from blybot.domain.models import PlatformCapabilities
from blybot.domain.ports import (
    PermanentTransportError,
    RateLimited,
    TransientTransportError,
)

if TYPE_CHECKING:
    from telegram import Bot

    from blybot.domain.models import OutboundMessage

# Telegram supports every capability the core knows about, so gating on
# these never changes Telegram behavior — the gates simply exist for the
# platforms (PR-5's Discord) that do not.
TELEGRAM_CAPABILITIES: Final = PlatformCapabilities(
    max_message_chars=4096,
    threads=True,
    durable_dm=True,
    deep_links=True,
    chat_picker=True,
    message_delete=True,
    id_can_change=True,
    rich_choices=True,
)


@dataclass(eq=False)
class TelegramTransport:
    """Sends :class:`OutboundMessage`s through python-telegram-bot.

    The AIORateLimiter that paces sends under Telegram's ceilings lives on
    ``bot`` (a Telegram-adapter concern); this class only maps the SDK
    errors that still leak through onto the abstract send taxonomy.
    """

    bot: Bot

    @property
    def capabilities(self) -> PlatformCapabilities:
        """Telegram's capability descriptor."""
        return TELEGRAM_CAPABILITIES

    async def send(self, message: OutboundMessage) -> None:
        """Deliver one message, mapping PTB errors to the transport taxonomy.

        ``RetryAfter`` becomes :class:`RateLimited` (its wait preserved),
        ``TimedOut`` a :class:`TransientTransportError`, and every other
        ``TelegramError`` (kicked, muted, chat-not-found) a
        :class:`PermanentTransportError`. The three PTB flood/timeout types
        are subclasses of ``TelegramError``, so they are caught first.
        """
        chat_id, thread_id = telegram_target(message.scope)
        try:
            await send_threaded(self.bot, chat_id, thread_id or 0, message.text)
        except RetryAfter as exc:
            # retry_after is a timedelta under PTB_TIMEDELTA (an int on the
            # legacy path); normalize both to a timedelta.
            wait = exc.retry_after
            seconds = wait.total_seconds() if isinstance(wait, timedelta) else wait
            raise RateLimited(timedelta(seconds=seconds)) from exc
        except TimedOut as exc:
            raise TransientTransportError from exc
        except TelegramError as exc:
            raise PermanentTransportError from exc
