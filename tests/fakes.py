"""In-memory fakes implementing the domain ports for unit tests."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta

from blybot.domain.models import (
    ActionContext,
    ActionSpec,
    CapturedMessage,
    GroupProfile,
    OutboundMessage,
    PlatformCapabilities,
    PromptRequest,
    PromptResult,
    Pseudonym,
    RepoEvent,
    RepoSummary,
    Resource,
    Scope,
    StepSpec,
)
from blybot.domain.ports import (
    IssueTrackerError,
    PermanentTransportError,
    StorageError,
    WikiWriteError,
)
from blybot.domain.subscriptions import Subscription


@dataclass
class FakeTransport:
    """Transport fake: records sends; permanently-fails chosen scope keys.

    ``capabilities`` defaults to a bare cap so tests that only exercise the
    delivery loop need not spell out a full descriptor.
    """

    capabilities: PlatformCapabilities = field(
        default_factory=lambda: PlatformCapabilities(max_message_chars=4096)
    )
    sent: list[OutboundMessage] = field(default_factory=list)
    permanent_fail_keys: set[str] = field(default_factory=set)

    async def send(self, message: OutboundMessage) -> None:
        if message.scope.key in self.permanent_fail_keys:
            raise PermanentTransportError
        self.sent.append(message)


@dataclass
class FakePublisher:
    """Records discussion writes instead of hitting the network.

    Each recorded entry is ``(page, heading, text, summary)``; ``started``
    holds new sections, ``continued`` holds appends into existing ones.
    """

    started: list[tuple[str, str, str, str]] = field(default_factory=list)
    continued: list[tuple[str, str, str, str]] = field(default_factory=list)
    uploads: list[tuple[str, bytes, str, str, str]] = field(default_factory=list)

    async def start_discussion(self, page: str, heading: str, text: str, summary: str) -> None:
        self.started.append((page, heading, text, summary))

    async def continue_discussion(self, page: str, heading: str, text: str, summary: str) -> None:
        self.continued.append((page, heading, text, summary))

    async def upload_file(
        self, filename: str, content: bytes, content_type: str, summary: str, description: str
    ) -> str:
        self.uploads.append((filename, content, content_type, summary, description))
        return filename

    @property
    def wrote_nothing(self) -> bool:
        """Whether no text reached the wiki through ANY write operation.

        Emptiness checks must cover both operations, or a handler
        switched to the other write path would slip past them.
        """
        return not self.started and not self.continued and not self.uploads


class FailingPublisher:
    """Publisher whose every write fails (as if retries were exhausted)."""

    async def _fail(self, page: str, heading: str, text: str, summary: str) -> None:
        del page, heading, text, summary
        raise WikiWriteError

    start_discussion = _fail
    continue_discussion = _fail

    async def upload_file(
        self, filename: str, content: bytes, content_type: str, summary: str, description: str
    ) -> str:
        del filename, content, content_type, summary, description
        raise WikiWriteError


@dataclass
class FlakyPublisher:
    """Fails the first ``failures`` writes, then records like :class:`FakePublisher`."""

    failures: int = 1
    recorder: FakePublisher = field(default_factory=FakePublisher)

    async def start_discussion(self, page: str, heading: str, text: str, summary: str) -> None:
        self._maybe_fail()
        await self.recorder.start_discussion(page, heading, text, summary)

    async def continue_discussion(self, page: str, heading: str, text: str, summary: str) -> None:
        self._maybe_fail()
        await self.recorder.continue_discussion(page, heading, text, summary)

    async def upload_file(
        self, filename: str, content: bytes, content_type: str, summary: str, description: str
    ) -> str:
        self._maybe_fail()
        return await self.recorder.upload_file(
            filename, content, content_type, summary, description
        )

    def _maybe_fail(self) -> None:
        if self.failures > 0:
            self.failures -= 1
            raise WikiWriteError


class PassthroughSanitizer:
    """Marks text so tests can assert sanitization happened before publish."""

    def sanitize(self, text: str) -> str:
        return f"[sanitized]{text}"


@dataclass
class FakeClock:
    """Manually advanced clock for deterministic TTL tests."""

    current: datetime = field(default_factory=lambda: datetime(2026, 7, 10, 12, 0, tzinfo=UTC))

    def now(self) -> datetime:
        return self.current

    def advance(self, delta: timedelta) -> None:
        self.current += delta


@dataclass
class ScriptedPseudonyms:
    """Returns a fixed sequence of pseudonym values, then repeats the last."""

    values: list[str] = field(default_factory=list)
    _position: int = 0

    def mint(self) -> Pseudonym:
        value = self.values[min(self._position, len(self.values) - 1)]
        self._position += 1
        return Pseudonym(value)


@dataclass
class SequentialPseudonyms:
    """Deterministic pseudonym factory: Anon-1, Anon-2, ..."""

    counter: int = 0

    def mint(self) -> Pseudonym:
        self.counter += 1
        return Pseudonym(f"Anon-{self.counter}")


@dataclass
class InMemoryProfiles:
    """ProfileStore + TokenVault keyed by :class:`Scope`."""

    profiles: dict[Scope, GroupProfile] = field(default_factory=dict)
    tokens: dict[Scope, str] = field(default_factory=dict)
    cursors: dict[Scope, dict[str, str]] = field(default_factory=dict)
    fail: bool = False
    fail_upserts: bool = False
    fail_token_writes: bool = False
    fail_token_reads: bool = False

    def _check(self) -> None:
        if self.fail:
            raise StorageError

    async def get(self, scope: Scope) -> GroupProfile | None:
        self._check()
        profile = self.profiles.get(scope)
        if profile is None:
            return None
        return replace(profile, has_token=scope in self.tokens)

    async def get_by_subscribe_code(self, code: str) -> GroupProfile | None:
        self._check()
        for key, profile in self.profiles.items():
            if profile.subscribe_code == code:
                return replace(profile, has_token=key in self.tokens)
        return None

    async def upsert(self, profile: GroupProfile) -> None:
        self._check()
        if self.fail_upserts:
            raise StorageError
        self.profiles[profile.scope] = profile

    async def delete(self, scope: Scope) -> None:
        self._check()
        self.profiles.pop(scope, None)
        self.tokens.pop(scope, None)
        self.cursors.pop(scope, None)

    async def list_event_enabled(self) -> list[GroupProfile]:
        self._check()
        return [
            replace(profile, has_token=key in self.tokens)
            for key, profile in self.profiles.items()
            if profile.events_enabled
        ]

    async def list_capture_enabled(self) -> list[GroupProfile]:
        self._check()
        return [profile for profile in self.profiles.values() if profile.capture_enabled]

    async def get_cursors(self, scope: Scope) -> dict[str, str]:
        self._check()
        return dict(self.cursors.get(scope, {}))

    async def set_cursors(self, scope: Scope, cursors: dict[str, str], repo: str = "") -> None:
        self._check()
        profile = self.profiles.get(scope)
        if repo and (profile is None or profile.repo != repo):
            return  # repo guard: stale in-flight cursor writes are dropped
        self.cursors[scope] = dict(cursors)

    async def migrate(self, old: Scope, new: Scope) -> None:
        self._check()
        for key in list(self.profiles):
            if key.channel == old.channel:
                moved = replace(key, channel=new.channel)
                self.profiles[moved] = replace(self.profiles.pop(key), scope=moved)
        for mapping in (self.tokens, self.cursors):
            for key in list(mapping):
                if key.channel == old.channel:
                    mapping[replace(key, channel=new.channel)] = mapping.pop(key)  # type: ignore[assignment]

    async def store_token(self, scope: Scope, token: str) -> None:
        self._check()
        if self.fail_token_writes:
            raise StorageError
        self.tokens[scope] = token

    async def fetch_token(self, scope: Scope) -> str | None:
        self._check()
        if self.fail_token_reads:
            raise StorageError
        return self.tokens.get(scope)

    async def delete_token(self, scope: Scope) -> None:
        self._check()
        self.tokens.pop(scope, None)


@dataclass
class FakeRepoGateway:
    """RepoGateway fake: configurable validation, recorded issues."""

    valid_tokens: set[str] = field(default_factory=set)
    issues: list[tuple[str, str, str, str]] = field(default_factory=list)
    summaries: dict[str, RepoSummary] = field(default_factory=dict)
    events: list[RepoEvent] = field(default_factory=list)
    fail: bool = False

    async def validate_token(self, repo: str, token: str) -> bool:
        del repo
        return token in self.valid_tokens

    async def open_issue(self, repo: str, token: str, title: str, body: str) -> str:
        if self.fail or token not in self.valid_tokens:
            raise IssueTrackerError
        self.issues.append((repo, token, title, body))
        return f"https://github.com/{repo}/issues/{len(self.issues)}"

    async def open_summary(self, repo: str, token: str) -> RepoSummary:
        if self.fail or token not in self.valid_tokens:
            raise IssueTrackerError
        return self.summaries.get(
            repo, RepoSummary(repo=repo, open_count=2, recent_titles=("A", "B"))
        )

    async def poll_resource(
        self, repo: str, token: str, resource: Resource, cursor: str | None
    ) -> tuple[list[RepoEvent], str]:
        del repo
        if self.fail or token not in self.valid_tokens:
            raise IssueTrackerError
        next_cursor = f"{resource.value}|next"
        if cursor is None:  # baseline: advance the cursor, emit nothing
            return [], next_cursor
        wanted = [event for event in self.events if event.event_type.resource is resource]
        return wanted, next_cursor


@dataclass
class FakeSource:
    """Source fake: returns a scripted payload, records each fetch."""

    payload: object | None = None
    fetched: list[ActionContext] = field(default_factory=list)

    async def fetch(self, context: ActionContext) -> object | None:
        self.fetched.append(context)
        return self.payload


@dataclass
class SuffixTransform:
    """Transform fake: appends its tag (or a step param) to a str payload."""

    tag: str = "+t"
    drop: bool = False  # simulate "nothing left to do"
    fail: bool = False
    applied: list[tuple[StepSpec, object]] = field(default_factory=list)

    async def apply(self, context: ActionContext, step: StepSpec, payload: object) -> object | None:
        del context
        if self.fail:
            msg = "transform exploded"
            raise RuntimeError(msg)
        self.applied.append((step, payload))
        if self.drop:
            return None
        return f"{payload}{step.param('tag', self.tag)}"


@dataclass
class FakeSink:
    """Sink fake: records deliveries, answers with scripted messages."""

    messages: tuple[OutboundMessage, ...] = ()
    fail: bool = False
    delivered: list[tuple[ActionContext, object]] = field(default_factory=list)

    async def deliver(self, context: ActionContext, payload: object) -> tuple[OutboundMessage, ...]:
        if self.fail:
            msg = "sink exploded"
            raise RuntimeError(msg)
        self.delivered.append((context, payload))
        return self.messages


@dataclass
class InMemoryActions:
    """ActionStore fake keyed by :class:`Scope`."""

    actions: dict[Scope, tuple[ActionSpec, ...]] = field(default_factory=dict)
    fail: bool = False
    fail_writes_for: set[Scope] = field(default_factory=set)

    async def get_actions(self, scope: Scope) -> tuple[ActionSpec, ...]:
        if self.fail:
            raise StorageError
        return self.actions.get(scope, ())

    async def set_actions(self, scope: Scope, actions: tuple[ActionSpec, ...]) -> None:
        if self.fail or scope in self.fail_writes_for:
            raise StorageError
        self.actions[scope] = actions

    async def list_scheduled(self) -> list[tuple[Scope, tuple[ActionSpec, ...]]]:
        if self.fail:
            raise StorageError
        return [(scope, specs) for scope, specs in self.actions.items() if specs]


@dataclass
class InMemorySubscriptions:
    """SubscriptionStore fake keyed by sub_id."""

    subs: dict[str, Subscription] = field(default_factory=dict)
    fail: bool = False

    def _check(self) -> None:
        if self.fail:
            raise StorageError

    async def add(self, subscription: Subscription) -> None:
        self._check()
        self.subs[subscription.sub_id] = subscription

    async def remove(self, dm: Scope, sub_id: str) -> bool:
        self._check()
        found = self.subs.get(sub_id)
        if found is None or found.dm != dm:
            return False
        del self.subs[sub_id]
        return True

    async def list_for_user(self, dm: Scope) -> list[Subscription]:
        self._check()
        mine = [s for s in self.subs.values() if s.dm == dm]
        return sorted(mine, key=lambda s: s.sub_id)

    async def list_all(self) -> list[Subscription]:
        self._check()
        return sorted(self.subs.values(), key=lambda s: s.sub_id)

    async def stamp(self, sub_id: str, last_run: datetime) -> None:
        self._check()
        if sub_id in self.subs:
            self.subs[sub_id] = replace(self.subs[sub_id], last_run=last_run)

    async def migrate(self, old: Scope, new: Scope) -> None:
        self._check()
        for sub_id, s in list(self.subs.items()):
            if s.scope.channel == old.channel:
                self.subs[sub_id] = replace(s, scope=replace(s.scope, channel=new.channel))


@dataclass
class InMemoryArchive:
    """MessageArchive fake: an ordered list with the adapter's semantics."""

    messages: list[CapturedMessage] = field(default_factory=list)
    fail: bool = False

    async def store(self, message: CapturedMessage) -> None:
        if self.fail:
            raise StorageError
        key = (message.scope, message.message_id)
        if any((m.scope, m.message_id) == key for m in self.messages):
            return  # idempotent per message id, like INSERT IGNORE
        self.messages.append(message)

    async def window(self, scope: Scope, since: datetime, until: datetime) -> list[CapturedMessage]:
        if self.fail:
            raise StorageError
        matching = [m for m in self.messages if m.scope == scope and since <= m.posted_at < until]
        return sorted(matching, key=lambda m: (m.posted_at, m.message_id))

    async def purge(self, scope: Scope, before: datetime | None = None) -> int:
        if self.fail:
            raise StorageError
        kept = [
            m
            for m in self.messages
            if m.scope != scope or (before is not None and m.posted_at >= before)
        ]
        removed = len(self.messages) - len(kept)
        self.messages = kept
        return removed

    async def total(self) -> int:
        if self.fail:
            raise StorageError
        return len(self.messages)

    async def migrate(self, old: Scope, new: Scope) -> None:
        if self.fail:
            raise StorageError
        self.messages = [
            replace(m, scope=replace(m.scope, channel=new.channel))
            if m.scope.channel == old.channel
            else m
            for m in self.messages
            if m.scope.channel != new.channel  # new id wins, like the adapter's clear
        ]


@dataclass
class FakePromptRunner:
    """PromptRunner fake: scripted results (or errors), recorded requests."""

    results: list[PromptResult | Exception] = field(default_factory=list)
    requests: list[PromptRequest] = field(default_factory=list)

    async def run(self, request: PromptRequest) -> PromptResult:
        self.requests.append(request)
        if not self.results:
            return PromptResult(content='["fallback item"]')
        result = self.results.pop(0)
        if isinstance(result, Exception):
            raise result
        return result
