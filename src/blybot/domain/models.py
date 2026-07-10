"""Value objects shared across the domain.

All types here are immutable and deliberately identifier-free: nothing in
this module can hold a Telegram user ID, username, or display name
(spec R6).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum


class TimestampGranularity(Enum):
    """How precisely on-page entries are timestamped (spec section 9)."""

    NONE = "none"
    DATE = "date"


@dataclass(frozen=True, slots=True)
class LogEntry:
    """A single sanitized message ready for publication to the group log."""

    text: str

    def __post_init__(self) -> None:
        if not self.text.strip():
            msg = "LogEntry text must be non-empty"
            raise ValueError(msg)


@dataclass(frozen=True, slots=True)
class Pseudonym:
    """A per-session anonymous handle, minted from a CSPRNG (spec R6, section 10)."""

    value: str

    def __post_init__(self) -> None:
        if not self.value:
            msg = "Pseudonym must be non-empty"
            raise ValueError(msg)


@dataclass(frozen=True, slots=True)
class Session:
    """An anonymized DM session.

    The session key used by callers is the private *chat id*, but the chat
    id itself lives only in the volatile registry keys, never inside this
    value object — so a ``Session`` can be passed to publication code
    without carrying any Telegram identifier.
    """

    pseudonym: Pseudonym
    anchor: str
    last_seen: datetime
