"""The IRC line protocol: parsing and formatting, with no I/O.

Hand-rolled rather than pulled from a library, deliberately. This project
enforces ``mypy --strict`` and 100% branch coverage; the maintained async
IRC clients ship no ``py.typed``, and testing through one means mocking
its internals rather than exercising ours. The grammar below is small
enough (RFC 1459 §2.3) that owning it costs less than wrapping someone
else's — and it stays pure, so every branch is reachable from a string.

A server line is::

    [':' prefix SPACE] command [params] [' :' trailing] CRLF

The prefix is the sender; for a channel message it is ``nick!user@host``,
from which only the nick is taken (and immediately pseudonymized at the
capture boundary — see :mod:`~blybot.adapters.irc.author_mask`).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

# The protocol line limit, CRLF included (RFC 1459 §2.3).
LINE_BYTES: Final = 512
_CRLF: Final = "\r\n"


@dataclass(frozen=True, slots=True)
class IrcLine:
    """One parsed server line."""

    prefix: str
    command: str
    params: tuple[str, ...]
    trailing: str

    @property
    def nick(self) -> str:
        """The sender's nick, or ``""`` when the line carries no prefix.

        A prefix is ``nick!user@host`` for a user and a bare server name
        otherwise; a server name has no ``!``, and attributing a message to
        one would be wrong, so it reads as no nick.
        """
        if "!" not in self.prefix:
            return ""
        return self.prefix.split("!", 1)[0]

    @property
    def target(self) -> str:
        """The first parameter — for PRIVMSG, the channel or nick addressed."""
        return self.params[0] if self.params else ""


def parse_line(raw: str) -> IrcLine | None:
    """Parse one server line; ``None`` when it carries no command.

    Blank keepalives and malformed fragments read as ``None`` rather than
    raising: a peer can send anything, and one bad line must not take down
    the read loop.
    """
    line = raw.strip(_CRLF).strip()
    if not line:
        return None
    prefix = ""
    if line.startswith(":"):
        prefix, _, line = line[1:].partition(" ")
        if not line:
            return None
    head, _, trailing = line.partition(" :")
    parts = head.split()
    if not parts:
        return None
    return IrcLine(
        prefix=prefix, command=parts[0].upper(), params=tuple(parts[1:]), trailing=trailing
    )


def privmsg_lines(target: str, text: str) -> list[str]:
    """Render ``text`` as PRIVMSG lines that fit the protocol's byte budget.

    The 512-byte cap covers the whole line, so the budget for the payload
    is what remains after ``PRIVMSG <target> :`` and CRLF — which depends
    on the target's length. Splitting is by **encoded bytes**, not
    characters, and never mid-codepoint: a chunk cut inside a multi-byte
    sequence would reach the channel as mojibake.

    Empty text yields no lines: IRC treats an empty trailing as a protocol
    error on some servers, and there is nothing to say.
    """
    budget = LINE_BYTES - len(f"PRIVMSG {target} :".encode()) - len(_CRLF)
    lines: list[str] = []
    for paragraph in text.split("\n"):
        lines.extend(f"PRIVMSG {target} :{chunk}" for chunk in _split_bytes(paragraph, budget))
    return lines


def _split_bytes(text: str, budget: int) -> list[str]:
    """Split ``text`` into pieces whose UTF-8 encoding fits ``budget`` bytes."""
    if not text:
        return []
    pieces: list[str] = []
    current = ""
    size = 0
    for char in text:
        width = len(char.encode())
        if size + width > budget:
            pieces.append(current)
            current, size = "", 0
        current += char
        size += width
    pieces.append(current)  # non-empty: the loop ran, and a flush is always refilled
    return pieces
