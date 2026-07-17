"""In-memory fakes implementing the domain ports for unit tests."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta

from blybot.domain.models import GroupProfile, Pseudonym, RepoEvent, RepoSummary, Resource
from blybot.domain.ports import IssueTrackerError, StorageError, WikiWriteError


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
    """ProfileStore + TokenVault keyed by (chat_id, thread_id)."""

    profiles: dict[tuple[int, int], GroupProfile] = field(default_factory=dict)
    tokens: dict[tuple[int, int], str] = field(default_factory=dict)
    cursors: dict[tuple[int, int], dict[str, str]] = field(default_factory=dict)
    fail: bool = False
    fail_upserts: bool = False
    fail_token_writes: bool = False
    fail_token_reads: bool = False

    def _check(self) -> None:
        if self.fail:
            raise StorageError

    async def get(self, chat_id: int, thread_id: int) -> GroupProfile | None:
        self._check()
        key = (chat_id, thread_id)
        profile = self.profiles.get(key)
        if profile is None:
            return None
        return replace(profile, has_token=key in self.tokens)

    async def upsert(self, profile: GroupProfile) -> None:
        self._check()
        if self.fail_upserts:
            raise StorageError
        self.profiles[profile.chat_id, profile.thread_id] = profile

    async def delete(self, chat_id: int, thread_id: int) -> None:
        self._check()
        key = (chat_id, thread_id)
        self.profiles.pop(key, None)
        self.tokens.pop(key, None)
        self.cursors.pop(key, None)

    async def list_event_enabled(self) -> list[GroupProfile]:
        self._check()
        return [
            replace(profile, has_token=key in self.tokens)
            for key, profile in self.profiles.items()
            if profile.events_enabled
        ]

    async def get_cursors(self, chat_id: int, thread_id: int) -> dict[str, str]:
        self._check()
        return dict(self.cursors.get((chat_id, thread_id), {}))

    async def set_cursors(
        self, chat_id: int, thread_id: int, cursors: dict[str, str], repo: str = ""
    ) -> None:
        self._check()
        key = (chat_id, thread_id)
        profile = self.profiles.get(key)
        if repo and (profile is None or profile.repo != repo):
            return  # repo guard: stale in-flight cursor writes are dropped
        self.cursors[key] = dict(cursors)

    async def migrate(self, old_chat_id: int, new_chat_id: int) -> None:
        self._check()
        for mapping in (self.profiles, self.tokens, self.cursors):
            for chat_id, thread_id in list(mapping):
                if chat_id == old_chat_id:
                    value = mapping.pop((chat_id, thread_id))
                    if isinstance(value, GroupProfile):
                        value = replace(value, chat_id=new_chat_id)
                    mapping[new_chat_id, thread_id] = value  # type: ignore[assignment]

    async def store_token(self, chat_id: int, thread_id: int, token: str) -> None:
        self._check()
        if self.fail_token_writes:
            raise StorageError
        self.tokens[chat_id, thread_id] = token

    async def fetch_token(self, chat_id: int, thread_id: int) -> str | None:
        self._check()
        if self.fail_token_reads:
            raise StorageError
        return self.tokens.get((chat_id, thread_id))

    async def delete_token(self, chat_id: int, thread_id: int) -> None:
        self._check()
        self.tokens.pop((chat_id, thread_id), None)


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
