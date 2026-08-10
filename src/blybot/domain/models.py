"""Value objects shared across the domain.

All types here are immutable and deliberately identifier-free: nothing in
this module can hold a Telegram user ID, username, or display name
(spec R6).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum
from typing import Final


class TimestampGranularity(Enum):
    """How precisely on-page section headings are timestamped (spec section 9).

    ``MINUTE`` adds HH:MM UTC; the wiki's edit history exposes precise
    times regardless, so the correlation cost is marginal.
    """

    NONE = "none"
    DATE = "date"
    MINUTE = "minute"


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
class Scope:
    """An opaque, platform-tagged address for one channel/topic a pipeline serves.

    Replaces the Telegram-specific ``(chat_id:int, thread_id:int)`` pair. All
    three parts are **opaque strings** the owning platform adapter mints and
    interprets — the core never parses them, only compares and stores them.
    ``thread`` is ``""`` when the platform has no threads, or the address is the
    channel default (what Telegram encoded as ``thread_id == 0``).

    A direct-message target is just a ``Scope`` whose ``channel`` is the DM
    handle: on Telegram that handle equals the user id (the R6 subscription
    carve-out), on Discord it is the DM channel id — distinct from the user id.
    This keeps a single addressing type for both group and DM delivery.
    """

    platform: str
    channel: str
    thread: str = ""

    def __post_init__(self) -> None:
        if not self.platform:
            msg = "Scope.platform must be non-empty"
            raise ValueError(msg)
        if not self.channel:
            msg = "Scope.channel must be non-empty"
            raise ValueError(msg)
        # The canonical key joins the parts on reserved separators; forbidding
        # those characters in the (opaque, adapter-minted) parts keeps the key
        # collision-free without escaping. Every platform's ids already avoid
        # them (Telegram ints, Discord/Slack snowflakes, IRC "#name").
        reserved = "".join(sorted(_SCOPE_SEPARATORS))
        for part in (self.platform, self.channel, self.thread):
            if _SCOPE_SEPARATORS & set(part):
                msg = f"Scope parts must not contain {reserved!r}: {part!r}"
                raise ValueError(msg)

    @property
    def key(self) -> str:
        """A stable, collision-free string key for dict/registry/log use."""
        return f"{self.platform}:{self.channel}/{self.thread}"


_SCOPE_SEPARATORS: Final = frozenset(":/")


@dataclass(frozen=True, slots=True)
class PlatformCapabilities:
    """What a chat platform's adapter can and cannot do; features gate on it.

    A frozen descriptor each transport adapter exposes so platform-agnostic
    services and handlers can gate Telegram-shaped features (durable DMs,
    message deletion, deep-link onboarding, supergroup-style id changes)
    without importing the adapter. ``max_message_chars`` is the platform's
    hard per-message character cap, injected wherever outbound text must be
    bounded, so no service hard-codes one platform's limit.
    """

    max_message_chars: int
    threads: bool = False
    durable_dm: bool = False
    deep_links: bool = False
    # Whether the BOT may open a DM with a user unprompted. This is the fact
    # that shapes the whole DM-transcription flow, and it was previously
    # mis-modelled as ``chat_picker`` — the name of Telegram's picker WIDGET
    # rather than the reason one is needed. A Telegram bot cannot message a
    # user who has not written to it first, so the user must come to the bot,
    # arriving with no indication of which channel prompted them: hence both
    # the deep link and the destination picker. Where the bot can open the DM
    # (Discord), the flow starts in the target channel and the destination is
    # already known, so neither is required.
    bot_can_open_dm: bool = False
    message_delete: bool = False
    id_can_change: bool = False  # supergroup-migration-style id changes
    rich_choices: bool = False
    # Whether the platform offers ANY way for a user to hand the bot a
    # secret without leaving it sitting in a chat log. Telegram has one
    # (paste into DM, bot deletes the message); Discord has a better one (a
    # modal, which never becomes a message at all). IRC has none: a channel
    # line is public, a private message still lands in the sender's client
    # log and usually a bouncer, and ``message_delete=False`` rules out
    # cleaning up after either. Where this is False the token-entry command
    # must refuse rather than degrade, because the degraded version is
    # "type your credential into a log file".
    confidential_input: bool = False

    def __post_init__(self) -> None:
        if self.max_message_chars <= 0:
            msg = "PlatformCapabilities.max_message_chars must be positive"
            raise ValueError(msg)


@dataclass(frozen=True, slots=True)
class CommandResult:
    """The platform-neutral outcome of a shared admin command.

    A frozen value each transport adapter renders into its native reply:
    the command's business logic lives once in a neutral service, and the
    adapter only maps its trigger to a call and sends ``text`` back. A
    Structured ``choices`` for platforms advertising ``rich_choices`` remain
    unbuilt on purpose: nothing needs them yet, and Telegram's one picker is
    a native chat-request button that generic choices cannot express. Adding
    the field before a consumer exists would be the same dead-config mistake
    ``chat_picker`` was (#45).

    ``payload`` carries the pipeline's own result when an adapter needs more
    than prose to finish the job — Telegram's ``/logmedia`` review DM needs
    the :class:`~blybot.services.publish.PublishedLog` to know which files
    were uploaded. It is deliberately ``object``: the neutral service does
    not care what a platform does with it, and typing it would drag a
    service type into the domain.

    ``ok`` marks whether the command actually did its thing (vs. a refusal,
    usage hint, or transient failure) so an adapter may decorate a *success*
    with a platform-specific affordance — e.g. Telegram appending its
    ``/capture purge`` hint — without re-implementing the outcome logic.
    """

    text: str
    ok: bool = True
    payload: object = None

    def __post_init__(self) -> None:
        if not self.text:
            msg = "CommandResult text must be non-empty"
            raise ValueError(msg)


@dataclass(frozen=True, slots=True)
class LogMedia:
    """One anonymous media attachment selected for wiki publication.

    The bytes are transient and deliberately carry no Telegram file id,
    original filename, author, or chat metadata. The publication service
    generates the public wiki filename from the log pseudonym.
    """

    content: bytes
    content_type: str

    def __post_init__(self) -> None:
        if not self.content:
            msg = "LogMedia content must be non-empty"
            raise ValueError(msg)
        if not self.content_type:
            msg = "LogMedia content_type must be non-empty"
            raise ValueError(msg)


@dataclass(frozen=True, slots=True)
class LogContent:
    """Publishable content from a ``/log`` target: optional text plus media."""

    text: str | None = None
    media: tuple[LogMedia, ...] = ()

    @property
    def has_publishable_content(self) -> bool:
        """Whether this log has text, media, or both."""
        return bool((self.text and self.text.strip()) or self.media)


@dataclass(frozen=True, slots=True)
class Transcript:
    """An archive window handed to analysis transforms — pseudonymized already."""

    messages: tuple[CapturedMessage, ...]
    since: datetime
    until: datetime


@dataclass(frozen=True, slots=True)
class AnalysisReport:
    """The structured outcome of an LLM analysis, ready for trusted rendering.

    ``items`` are untrusted model-written strings: every consumer must
    sanitize them individually before they may touch a wiki page (v3
    plan §2.5 — the model never writes markup).
    """

    title: str
    items: tuple[str, ...]
    message_count: int
    participant_count: int
    since: datetime
    until: datetime
    model_label: str
    lang: str
    sampled: bool = False  # the window exceeded the chunk cap; items cover a prefix


@dataclass(frozen=True, slots=True)
class StatsReport:
    """Deterministic activity statistics — computed in Python, no LLM involved."""

    message_count: int
    participant_count: int
    media_count: int
    reply_count: int
    busiest_hour_utc: int | None
    busiest_hour_count: int
    top_authors: tuple[tuple[str, int], ...]  # (pseudonym label, messages)
    since: datetime
    until: datetime


@dataclass(frozen=True, slots=True)
class GroupProfile:
    """Self-service configuration one group's admins chose from Telegram.

    The only identifiers the bot persists are group structure — the
    :class:`Scope` (platform + channel + optional topic thread) — never a
    user id, name, or message. ``None`` fields fall back to the group
    default and then the operator's environment defaults.
    """

    scope: Scope
    log_page: str | None = None
    repo: str | None = None
    consent_mode: ConsentMode | None = None
    events_enabled: bool = False
    rules: tuple[Rule, ...] = ()
    has_token: bool = False
    # Tri-state: True/False are explicit /capture decisions; None means
    # "never decided", which lets a forum topic inherit the group default
    # while an explicit topic-level off still beats an enabled group.
    capture_enabled: bool | None = None
    llm: LlmSettings | None = None
    # Non-null ⇒ the scope is subscribable; the value is the random
    # deep-link capability code an admin minted with /subscribable on.
    subscribe_code: str | None = None


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
            and (
                not self.assignee
                or self.assignee.casefold() in {a.casefold() for a in event.assignees}
            )
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


WEEKDAY_TOKENS: Final = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")

_HOURS_PER_DAY: Final = 24
_MINUTES_PER_HOUR: Final = 60
_DAYS_PER_WEEK: Final = 7


class TriggerKind(Enum):
    """How an action starts: an explicit chat command or the schedule tick."""

    COMMAND = "command"
    SCHEDULE = "schedule"


@dataclass(frozen=True, slots=True)
class Schedule:
    """When a scheduled action is due. Pure aware-UTC math, no I/O.

    The three kinds mirror the ``/action`` grammar: ``every:<N>h``,
    ``daily@HH:MM``, and ``weekly@<dow>.HH:MM``. ``weekday`` follows
    :meth:`datetime.date.weekday` (0 = Monday).
    """

    kind: str  # "every" | "daily" | "weekly"
    interval_hours: int = 0
    hour: int = 0
    minute: int = 0
    weekday: int = 0

    def __post_init__(self) -> None:
        if self.kind not in {"every", "daily", "weekly"}:
            msg = f"unknown schedule kind: {self.kind!r}"
            raise ValueError(msg)
        if self.kind == "every" and self.interval_hours <= 0:
            msg = "every-schedules need a positive hour interval"
            raise ValueError(msg)
        if not (0 <= self.hour < _HOURS_PER_DAY and 0 <= self.minute < _MINUTES_PER_HOUR):
            msg = "schedule time must be a valid HH:MM"
            raise ValueError(msg)
        if not 0 <= self.weekday < _DAYS_PER_WEEK:
            msg = "schedule weekday must be 0 (mon) through 6 (sun)"
            raise ValueError(msg)

    @property
    def token(self) -> str:
        """The grammar token this schedule round-trips to."""
        if self.kind == "every":
            return f"every:{self.interval_hours}h"
        if self.kind == "daily":
            return f"daily@{self.hour:02d}:{self.minute:02d}"
        return f"weekly@{WEEKDAY_TOKENS[self.weekday]}.{self.hour:02d}:{self.minute:02d}"

    def is_due(self, now: datetime, last_run: datetime) -> bool:
        """Whether a run is owed at ``now`` given the previous ``last_run``.

        ``every`` measures elapsed time; ``daily``/``weekly`` fire once
        per slot — due when the most recent slot falls after the last
        run. A missed slot (downtime) is made up exactly once, never
        replayed per-slot.
        """
        if self.kind == "every":
            return now - last_run >= timedelta(hours=self.interval_hours)
        return last_run < self._latest_slot(now) <= now

    def _latest_slot(self, now: datetime) -> datetime:
        candidate = now.replace(hour=self.hour, minute=self.minute, second=0, microsecond=0)
        if self.kind == "daily":
            return candidate if candidate <= now else candidate - timedelta(days=1)
        candidate -= timedelta(days=(now.weekday() - self.weekday) % _DAYS_PER_WEEK)
        return candidate if candidate <= now else candidate - timedelta(days=_DAYS_PER_WEEK)


@dataclass(frozen=True, slots=True)
class TriggerSpec:
    """What starts an action: a command name or a :class:`Schedule`."""

    kind: TriggerKind
    command: str = ""
    schedule: Schedule | None = None

    def __post_init__(self) -> None:
        if self.kind is TriggerKind.COMMAND and not self.command:
            msg = "command triggers need a command name"
            raise ValueError(msg)
        if self.kind is TriggerKind.SCHEDULE and self.schedule is None:
            msg = "schedule triggers need a schedule"
            raise ValueError(msg)


@dataclass(frozen=True, slots=True)
class StepSpec:
    """One named pipeline step (source, transform, or sink) plus its params.

    ``params`` is an ordered tuple of ``(key, value)`` pairs so the spec
    stays hashable and round-trips deterministically.
    """

    name: str
    params: tuple[tuple[str, str], ...] = ()

    def __post_init__(self) -> None:
        if not self.name:
            msg = "StepSpec name must be non-empty"
            raise ValueError(msg)

    def param(self, key: str, default: str = "") -> str:
        """Return the value of ``key``, or ``default`` when unset."""
        for name, value in self.params:
            if name == key:
                return value
        return default


@dataclass(frozen=True, slots=True)
class ActionSpec:
    """A reusable pipeline an admin configured: trigger → source → transforms → sink.

    ``last_run`` is per-action scheduler state (stamped in UTC); it
    travels with the spec so a scope's actions persist as one document.
    """

    action_id: str
    trigger: TriggerSpec
    source: StepSpec
    transforms: tuple[StepSpec, ...] = ()
    # More than one destination: a summary may be published to the wiki AND
    # fanned out to subscribers. Every sink receives the same final payload
    # and their messages are concatenated in order.
    sinks: tuple[StepSpec, ...] = (StepSpec(name="reply"),)
    last_run: datetime | None = None


@dataclass(frozen=True, slots=True)
class ActionContext:
    """Everything a pipeline step may know about the run it is part of."""

    scope: Scope
    spec: ActionSpec
    now: datetime


@dataclass(frozen=True, slots=True)
class OutboundMessage:
    """One chat message a pipeline asks the transport layer to send.

    ``scope`` addresses where it goes — a group/topic, or (for a private
    digest) a DM scope the collector re-targeted it to.
    """

    scope: Scope
    text: str


@dataclass(frozen=True, slots=True)
class RunOutcome:
    """What one engine run produced.

    ``payload`` is the final value handed to the sink (``None`` when the
    run ended quietly) — interactive callers read their results from it;
    ``messages`` are the sink's chat messages for the transport to send.
    """

    payload: object | None
    messages: tuple[OutboundMessage, ...] = ()


@dataclass(frozen=True, slots=True)
class RepoEventsBatch:
    """One poll cycle's fresh events for a scope, with the rules to match."""

    repo: str
    rules: tuple[Rule, ...]
    events: tuple[RepoEvent, ...]


@dataclass(frozen=True, slots=True)
class LlmSettings:
    """Per-scope LLM configuration (v3 plan §2.4).

    ``model`` is an alias (``default`` or ``large``) the platform adapter
    resolves to a concrete id, so operators can re-point aliases without
    touching any scope's settings. ``lang`` pins the *output* language of
    every analysis regardless of the transcript's language(s).
    """

    platform: str = "liftwing"
    model: str = "default"
    lang: str = "en"
    temperature: float = 0.2
    max_tokens: int = 1024

    def __post_init__(self) -> None:
        if not (self.platform and self.model and self.lang):
            msg = "LlmSettings fields must be non-empty"
            raise ValueError(msg)
        if not 0.0 <= self.temperature <= 1.0:
            msg = "temperature must be between 0 and 1"
            raise ValueError(msg)
        if self.max_tokens <= 0:
            msg = "max_tokens must be positive"
            raise ValueError(msg)


@dataclass(frozen=True, slots=True)
class PromptRequest:
    """One text-in/text-out inference call, platform-agnostic."""

    model: str  # an alias the platform resolves (never a chat identity)
    system: str
    user_content: str
    max_tokens: int = 1024
    temperature: float = 0.2


@dataclass(frozen=True, slots=True)
class PromptResult:
    """What came back: content plus the safety-relevant metadata.

    ``finish_reason`` matters because ``"length"`` marks truncated output
    that must never be published; token counts feed operator counters.
    """

    content: str
    finish_reason: str = "stop"
    prompt_tokens: int = 0
    completion_tokens: int = 0


CAPTURE_KINDS: Final = frozenset({"text", "media_note"})


@dataclass(frozen=True, slots=True)
class CapturedMessage:
    """One archived channel/group message, pseudonymized at the boundary.

    ``author`` is a short per-scope pseudonym label the capture boundary
    derives (HMAC) — never a Telegram user id, username, or display
    name; a broadcast channel post has no author (``""``). ``kind`` is
    ``text`` or ``media_note`` (media itself is not archived; the note
    only feeds activity stats). ``scope`` is group structure, which the
    profile store already persists (spec 11).
    """

    scope: Scope
    message_id: int
    posted_at: datetime
    author: str = ""
    kind: str = "text"
    text: str = ""
    reply_to: int | None = None

    def __post_init__(self) -> None:
        if self.kind not in CAPTURE_KINDS:
            msg = f"unknown captured-message kind: {self.kind!r}"
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

    ``message_count`` is how many messages the session has recorded; on a
    talk page it doubles as the indentation depth of the next line, which
    is what renders the back-and-forth of a discussion.
    """

    pseudonym: Pseudonym
    anchor: str
    last_seen: datetime
    created_at: datetime
    message_count: int = 0
