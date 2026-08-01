"""The IRC socket: a thin, injectable wrapper around an asyncio stream.

Separated from the transport and the gateway so both can be tested without
a network. Everything that touches the socket lives here; everything above
it speaks lines.
"""

from __future__ import annotations

import asyncio
import time
from contextlib import suppress
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Protocol

from blybot.adapters.irc.protocol import LINE_BYTES, IrcLine, parse_line
from blybot.domain.ports import TransientTransportError

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Awaitable, Callable


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
    # Flood pacing. IRC servers kill a client that sends too fast ("excess
    # flood"), and unlike an HTTP 429 there is no warning and no retry-after
    # — the socket simply closes. A digest split across a dozen PRIVMSG
    # lines would trip it, so every write goes through a token bucket: a
    # short burst is allowed (servers expect one during registration), then
    # one line per `min_interval`. Defaults are conservative; the cost of
    # being slow is latency, the cost of being fast is a disconnect.
    burst: int = 4
    min_interval: float = 2.0
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep
    monotonic: Callable[[], float] = time.monotonic
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock, init=False)
    _allowance: float = field(default=0.0, init=False)
    _last_send: float = field(default=0.0, init=False)

    def __post_init__(self) -> None:
        self._allowance = float(self.burst)
        self._last_send = self.monotonic()

    async def send_line(self, line: str) -> None:
        """Write one line, paced and serialized against concurrent senders."""
        async with self._lock:
            await self._await_turn()
            try:
                self.writer.write(f"{line}\r\n".encode())
                await self.writer.drain()
            except (OSError, ConnectionError) as error:
                raise TransientTransportError from error

    async def _await_turn(self) -> None:
        """Block until the bucket has room for one more line."""
        now = self.monotonic()
        allowance = min(
            float(self.burst), self._allowance + (now - self._last_send) / self.min_interval
        )
        if allowance < 1.0:
            wait = (1.0 - allowance) * self.min_interval
            await self.sleep(wait)
            # Charge the wait to the clock: leaving `_last_send` at the
            # pre-sleep instant would credit the wait back as refill and let
            # the very next line through free, doubling the actual rate.
            self._allowance, self._last_send = 0.0, now + wait
        else:
            self._allowance, self._last_send = allowance - 1.0, now

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
