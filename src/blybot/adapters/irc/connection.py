"""The IRC socket: a thin, injectable wrapper around an asyncio stream.

Separated from the transport and the gateway so both can be tested without
a network. Everything that touches the socket lives here; everything above
it speaks lines.
"""

from __future__ import annotations

import asyncio
from contextlib import suppress
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Protocol

from blybot.adapters.irc.protocol import LINE_BYTES, IrcLine, parse_line
from blybot.domain.ports import TransientTransportError

if TYPE_CHECKING:
    from collections.abc import AsyncIterator


class LineChannel(Protocol):
    """A bidirectional line channel — what the gateway and transport need."""

    async def send_line(self, line: str) -> None:
        """Write one protocol line (CRLF appended here)."""
        ...

    def lines(self) -> AsyncIterator[IrcLine]:
        """Yield parsed inbound lines until the peer closes."""
        ...


@dataclass(eq=False)
class IrcConnection:
    """An open IRC session over an asyncio stream pair.

    A dropped connection surfaces as :class:`TransientTransportError`, so
    the shared delivery loop retries it exactly as it retries a Discord 5xx
    — reconnection policy is the loop's, not this class's.
    """

    reader: asyncio.StreamReader
    writer: asyncio.StreamWriter
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock, init=False)

    async def send_line(self, line: str) -> None:
        """Write one line, serialized so concurrent senders cannot interleave."""
        async with self._lock:
            try:
                self.writer.write(f"{line}\r\n".encode())
                await self.writer.drain()
            except (OSError, ConnectionError) as error:
                raise TransientTransportError from error

    async def lines(self) -> AsyncIterator[IrcLine]:
        """Yield parsed inbound lines until EOF.

        Over-long or malformed input is dropped rather than raised on: a
        peer can send anything, and one bad line must not end the session.
        """
        while True:
            try:
                raw = await self.reader.readline()
            except (OSError, ConnectionError) as error:
                raise TransientTransportError from error
            if not raw:
                return  # EOF: the peer closed
            parsed = parse_line(raw.decode("utf-8", errors="replace")[:LINE_BYTES])
            if parsed is not None:
                yield parsed

    async def close(self) -> None:
        """Close the stream, ignoring an already-broken pipe."""
        self.writer.close()
        with suppress(OSError, ConnectionError):
            await self.writer.wait_closed()
