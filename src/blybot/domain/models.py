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
    """How precisely on-page section headings are timestamped (spec section 9).

    ``MINUTE`` adds HH:MM UTC; the wiki's edit history exposes precise
    times regardless, so the correlation cost is marginal.
    """

    NONE = "none"
    DATE = "date"
    MINUTE = "minute"


class EventKind(Enum):
    """Repository happenings a group may subscribe to (polled, never pushed)."""

    RELEASES = "releases"
    PRS = "prs"
    ISSUES = "issues"


class ConsentMode(Enum):
    """Policy for publishing another person's message via ``/log`` (spec 17-18).

    ``CONFIRM`` is the N1 hook: the value is reserved and recognized, but
    the DM-confirmation flow is not implemented in v1 — configuring it is
    rejected at startup rather than silently degraded.
    """

    IMMEDIATE = "immediate"
    AUTHOR_ONLY = "author_only"
    CONFIRM = "confirm"


@dataclass(frozen=True, slots=True)
class GroupProfile:
    """Self-service configuration one group's admins chose from Telegram.

    This is the only identifier the bot ever persists: the group *chat
    id* (never a user id, name, or message). ``None`` fields fall back
    to the operator's environment defaults at resolution time.
    """

    chat_id: int
    log_page: str | None = None
    repo: str | None = None
    consent_mode: ConsentMode | None = None
    events_enabled: bool = False
    event_kinds: frozenset[EventKind] = frozenset()
    has_token: bool = False


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

    ``message_count`` is how many messages the session has recorded; on a
    talk page it doubles as the indentation depth of the next line, which
    is what renders the back-and-forth of a discussion.
    """

    pseudonym: Pseudonym
    anchor: str
    last_seen: datetime
    created_at: datetime
    message_count: int = 0
