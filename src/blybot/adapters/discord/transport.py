"""The Discord :class:`~blybot.domain.ports.Transport` implementation.

The outbound edge of the Discord adapter: it resolves a neutral
:class:`~blybot.domain.models.OutboundMessage`'s ``scope`` to a Discord
channel via the live client, sends the text (chunked at Discord's 2000-char
cap), and maps discord.py's SDK errors onto the platform-agnostic send
taxonomy the delivery loop reasons about. It also publishes Discord's
:class:`~blybot.domain.models.PlatformCapabilities` so features can gate
without importing anything Discord-specific.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from datetime import timedelta
from typing import TYPE_CHECKING, cast

import discord

from blybot.adapters.discord.capabilities import DISCORD_CAPABILITIES
from blybot.adapters.discord.scope import discord_target
from blybot.domain.ports import (
    PermanentTransportError,
    RateLimited,
    TransientTransportError,
)

if TYPE_CHECKING:
    from discord.abc import Messageable

    from blybot.domain.models import OutboundMessage, PlatformCapabilities

_HTTP_TOO_MANY_REQUESTS = 429


def _chunks(text: str, size: int) -> Iterator[str]:
    """Yield ``text`` in ``size``-character slices (Discord's per-message cap)."""
    for start in range(0, len(text), size):
        yield text[start : start + size]


@dataclass(eq=False)
class DiscordTransport:
    """Sends :class:`OutboundMessage`s through a live ``discord.Client``.

    Resolving a scope to a channel prefers the client's cache and falls
    back to a REST fetch; sending honours the 2000-char cap by chunking.
    discord.py paces its own HTTP under Discord's rate limits, so this
    class only maps the errors that still surface onto the abstract send
    taxonomy.
    """

    client: discord.Client

    @property
    def capabilities(self) -> PlatformCapabilities:
        """Discord's capability descriptor."""
        return DISCORD_CAPABILITIES

    async def send(self, message: OutboundMessage) -> None:
        """Deliver one message, mapping discord.py errors to the taxonomy.

        ``discord.RateLimited`` (and a leaked HTTP 429) become
        :class:`RateLimited`, ``DiscordServerError`` (5xx) and request
        timeouts a :class:`TransientTransportError`, and every other
        ``HTTPException`` (``Forbidden``, ``NotFound``, …) a
        :class:`PermanentTransportError`. ``DiscordServerError`` is a
        subclass of ``HTTPException``, so it is caught first.
        """
        channel_id, thread_id = discord_target(message.scope)
        target_id = thread_id if thread_id is not None else channel_id
        try:
            channel = await self._resolve(target_id)
            for chunk in _chunks(message.text, DISCORD_CAPABILITIES.max_message_chars):
                await channel.send(chunk)
        except discord.RateLimited as exc:
            raise RateLimited(timedelta(seconds=exc.retry_after)) from exc
        except discord.DiscordServerError as exc:
            raise TransientTransportError from exc
        except discord.HTTPException as exc:
            if exc.status == _HTTP_TOO_MANY_REQUESTS:
                # A 429 discord.py did not absorb itself: retry-after is not
                # carried on the plain HTTPException, so signal a wait of 0
                # and let the delivery loop's margin pace the retry.
                raise RateLimited(timedelta(0)) from exc
            raise PermanentTransportError from exc
        except TimeoutError as exc:
            raise TransientTransportError from exc

    async def _resolve(self, channel_id: int) -> Messageable:
        """Return the sendable channel for ``channel_id`` (cache, then fetch)."""
        channel = self.client.get_channel(channel_id)
        if channel is None:
            channel = await self.client.fetch_channel(channel_id)
        return cast("Messageable", channel)
