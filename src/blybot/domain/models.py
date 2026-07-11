"""Value objects shared across the domain.

All types here are immutable and deliberately identifier-free: nothing in
this module can hold a Telegram user ID, username, or display name
(spec R6).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
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


class Resource(Enum):
    """A GitHub resource stream the poller fetches to source events."""

    ISSUES = "issues"
    PULLS = "pulls"
    ISSUE_COMMENTS = "issue_comments"
    RELEASES = "releases"


class EventType(Enum):
    """A specific repository happening a rule can trigger on.

    ``value`` is the user-facing ``family.action`` token; ``resource``
    is the stream the poller reads to observe it. Every member here is
    reliably detectable from a resource's REST list endpoint by
    comparing item timestamps against the poll watermark.

    Fine-grained issue actions (reopened, labeled, assigned,
    milestoned) and ``pr.ready`` are deliberately absent: they are only
    reliably observable via the issue-events *timeline* API, a heavier
    per-resource source planned as a later enrichment (see the rules
    plan's deferred list). Their conditions are still expressible as
    *filters* — e.g. ``issue.opened label:bug`` — on the triggers below.
    """

    ISSUE_OPENED = ("issue.opened", Resource.ISSUES)
    ISSUE_CLOSED = ("issue.closed", Resource.ISSUES)
    PR_OPENED = ("pr.opened", Resource.PULLS)
    PR_CLOSED = ("pr.closed", Resource.PULLS)
    PR_MERGED = ("pr.merged", Resource.PULLS)
    COMMENT = ("comment", Resource.ISSUE_COMMENTS)
    RELEASE = ("release", Resource.RELEASES)

    def __init__(self, token: str, resource: Resource) -> None:
        self.token = token
        self.resource = resource

    @classmethod
    def from_token(cls, token: str) -> EventType:
        """Return the event type for a ``family.action`` token."""
        for member in cls:
            if member.token == token:
                return member
        msg = f"unknown event type: {token}"
        raise ValueError(msg)


class DeliveryMode(Enum):
    """How a rule's matches reach the chat."""

    LIVE = "live"  # one message per matching event, as it is found
    DIGEST = "digest"  # one combined message per poll cycle, silent if empty


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

    The only identifiers the bot persists are group structure — the
    *chat id* and (for forum groups) the *topic thread id* — never a
    user id, name, or message. ``None`` fields fall back to the group
    default and then the operator's environment defaults.
    """

    chat_id: int
    thread_id: int = 0  # forum topic; 0 = the group default (General)
    log_page: str | None = None
    repo: str | None = None
    consent_mode: ConsentMode | None = None
    events_enabled: bool = False
    event_kinds: frozenset[EventKind] = frozenset()
    rules: tuple[Rule, ...] = ()
    has_token: bool = False


@dataclass(frozen=True, slots=True)
class RepoEvent:
    """One repository happening, normalized to publishable + filterable facts.

    Carries only what rules filter on and messages render — never a
    Telegram identifier. ``author`` etc. are *GitHub* handles, which are
    public repository metadata, not chat identities (spec R6).
    """

    event_type: EventType
    title: str
    url: str
    author: str = ""
    labels: frozenset[str] = frozenset()
    base_branch: str = ""
    draft: bool = False
    assignees: frozenset[str] = frozenset()
    milestone: str = ""
    state: str = ""


@dataclass(frozen=True, slots=True)
class RuleFilter:
    """Composable conditions on a :class:`RepoEvent`.

    Distinct keys AND together; a comma-separated value is any-of (OR).
    ``title_match`` is a substring by default, or a regex when the
    parser wrapped it in slashes. An unset field never constrains.
    """

    labels: frozenset[str] = frozenset()
    author: str = ""
    base: str = ""
    title_match: str = ""
    title_is_regex: bool = False
    draft: bool | None = None
    assignee: str = ""
    milestone: str = ""

    def matches(self, event: RepoEvent) -> bool:
        """Whether ``event`` satisfies every set condition."""
        return (
            self._labels_ok(event)
            and (not self.author or self.author.casefold() == event.author.casefold())
            and (not self.base or self.base == event.base_branch)
            and self._title_ok(event)
            and (self.draft is None or self.draft == event.draft)
            and (not self.assignee or self.assignee in {a.casefold() for a in event.assignees})
            and (not self.milestone or self.milestone.casefold() == event.milestone.casefold())
        )

    def _labels_ok(self, event: RepoEvent) -> bool:
        if not self.labels:
            return True
        wanted = {label.casefold() for label in self.labels}
        present = {label.casefold() for label in event.labels}
        return bool(wanted & present)  # any-of

    def _title_ok(self, event: RepoEvent) -> bool:
        if not self.title_match:
            return True
        if self.title_is_regex:
            return re.search(self.title_match, event.title, re.IGNORECASE) is not None
        return self.title_match.casefold() in event.title.casefold()


@dataclass(frozen=True, slots=True)
class Rule:
    """A trigger + filter + delivery mode a scope watches for."""

    rule_id: str
    trigger: EventType
    filter: RuleFilter = field(default_factory=RuleFilter)
    mode: DeliveryMode = DeliveryMode.LIVE

    def matches(self, event: RepoEvent) -> bool:
        """Whether ``event`` fires this rule."""
        return event.event_type is self.trigger and self.filter.matches(event)


@dataclass(frozen=True, slots=True)
class RepoSummary:
    """A glance at a bound repository for the group's /repo command."""

    repo: str
    open_count: int
    recent_titles: tuple[str, ...]


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
