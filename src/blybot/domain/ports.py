"""Ports: the interfaces the domain and services depend on.

Adapters (Telegram, MediaWiki) implement these protocols; services depend
only on the protocols. This is the dependency-inversion seam of the
codebase — new transports or wiki clients plug in here without touching
business logic.
"""

from __future__ import annotations

from datetime import datetime
from typing import Protocol

from blybot.domain.models import (
    ActionContext,
    ActionScope,
    ActionSpec,
    CapturedMessage,
    GroupProfile,
    OutboundMessage,
    PromptRequest,
    PromptResult,
    Pseudonym,
    RepoEvent,
    RepoSummary,
    Resource,
    StepSpec,
)
from blybot.domain.subscriptions import Subscription


class WikiWriteError(Exception):
    """A wiki write failed after bounded retries.

    Defined in the domain so services can handle publish failures
    without importing the adapter that raised them.
    """


class IssueTrackerError(Exception):
    """Filing an issue with the tracker failed."""


class StorageError(Exception):
    """The profile store is unreachable or misbehaving.

    Defined in the domain so services and handlers can degrade
    gracefully without importing the database adapter.
    """


class ActionError(Exception):
    """An action could not run; the message is safe to show a group admin.

    Raised for configuration-shaped failures (unknown component names,
    unusable payloads) as opposed to transport errors, which keep their
    own exception types.
    """


class Source(Protocol):
    """Produces the payload an action pipeline starts from.

    Returning ``None`` means "nothing to do" — the engine stops the run
    quietly (an empty archive window is normal, not an error).
    """

    async def fetch(self, context: ActionContext) -> object | None:
        """Return the initial payload for this run, or ``None`` for no-op."""
        ...


class Transform(Protocol):
    """One payload → payload step of an action pipeline.

    ``step`` carries the parameters this occurrence was configured with,
    so one registered transform can serve many specs. Returning ``None``
    stops the run quietly.
    """

    async def apply(self, context: ActionContext, step: StepSpec, payload: object) -> object | None:
        """Return the transformed payload, or ``None`` to end the run."""
        ...


class Sink(Protocol):
    """Publishes an action pipeline's final payload.

    Sinks that write externally (wiki) return no messages; sinks that
    speak in chat return :class:`OutboundMessage`s for the transport
    layer to send — services never touch Telegram directly.
    """

    async def deliver(self, context: ActionContext, payload: object) -> tuple[OutboundMessage, ...]:
        """Publish ``payload``; return any chat messages to send."""
        ...


class PromptError(Exception):
    """An inference call failed for good after bounded retries.

    Defined in the domain so services can abort an analysis (and
    publish nothing) without importing the platform adapter.
    """


class PromptRunner(Protocol):
    """Text-in/text-out inference behind a platform adapter (v3 plan §2.3).

    Implementations resolve the request's model *alias* to a concrete
    id, apply transport retries, and surface truncation via
    ``finish_reason`` rather than guessing.
    """

    async def run(self, request: PromptRequest) -> PromptResult:
        """Execute one chat completion; raise :class:`PromptError` on failure."""
        ...


class MessageArchive(Protocol):
    """Persists captured messages for capture-enabled scopes (v3 plan §2.2).

    The archive holds pseudonymized structure and text only; nothing
    stored here can identify a Telegram account. Raising
    :class:`StorageError` lets callers degrade without importing the
    database adapter.
    """

    async def store(self, message: CapturedMessage) -> None:
        """Persist one captured message (idempotent per message id)."""
        ...

    async def window(
        self, chat_id: int, thread_id: int, since: datetime, until: datetime
    ) -> list[CapturedMessage]:
        """Return the scope's messages with ``since <= posted_at < until``, oldest first."""
        ...

    async def purge(self, chat_id: int, thread_id: int, before: datetime | None = None) -> int:
        """Hard-delete the scope's archive (older than ``before`` if given).

        Returns the rows removed.
        """
        ...

    async def total(self) -> int:
        """Return the archive's total row count (operator metric)."""
        ...

    async def migrate(self, old_chat_id: int, new_chat_id: int) -> None:
        """Re-key every topic's messages after a group→supergroup upgrade."""
        ...


class ActionStore(Protocol):
    """Persists each scope's configured actions (spec 11: one JSON document)."""

    async def get_actions(self, chat_id: int, thread_id: int) -> tuple[ActionSpec, ...]:
        """Return the (group, topic) actions, empty when none are configured."""
        ...

    async def set_actions(
        self, chat_id: int, thread_id: int, actions: tuple[ActionSpec, ...]
    ) -> None:
        """Replace the (group, topic) actions (state included) wholesale."""
        ...

    async def list_scheduled(self) -> list[tuple[ActionScope, tuple[ActionSpec, ...]]]:
        """Return every scope that has at least one action configured."""
        ...


class SubscriptionStore(Protocol):
    """Persists opt-in DM digest subscriptions (R6 carve-out, SPECIFICATION §21).

    The one durable store keyed by a subscriber's private chat id. Isolated
    from the profile/archive stores so the carve-out stays contained.
    """

    async def add(self, subscription: Subscription) -> None:
        """Persist a new subscription."""
        ...

    async def remove(self, dm_chat_id: int, sub_id: str) -> bool:
        """Delete the caller's subscription; return whether one existed."""
        ...

    async def list_for_user(self, dm_chat_id: int) -> list[Subscription]:
        """Return every subscription this DM chat owns, oldest first."""
        ...

    async def list_all(self) -> list[Subscription]:
        """Return every subscription (for the digest scheduler)."""
        ...

    async def stamp(self, sub_id: str, last_run: datetime) -> None:
        """Advance a subscription's ``last_run`` watermark durably."""
        ...

    async def migrate(self, old_chat_id: int, new_chat_id: int) -> None:
        """Re-key a group's subscriptions after a group→supergroup upgrade."""
        ...


class ProfileStore(Protocol):
    """Persists per-group self-service profiles (spec 11: ToolsDB)."""

    async def get(self, chat_id: int, thread_id: int) -> GroupProfile | None:
        """Return the (group, topic) profile, or ``None`` if unconfigured."""
        ...

    async def get_by_subscribe_code(self, code: str) -> GroupProfile | None:
        """Return the scope whose ``subscribe_code`` matches, or ``None``."""
        ...

    async def upsert(self, profile: GroupProfile) -> None:
        """Create or update the profile (token and cursor are untouched)."""
        ...

    async def delete(self, chat_id: int, thread_id: int) -> None:
        """Forget everything about the (group, topic), token and cursor included."""
        ...

    async def list_event_enabled(self) -> list[GroupProfile]:
        """Return every profile with repo notifications switched on."""
        ...

    async def list_capture_enabled(self) -> list[GroupProfile]:
        """Return every profile with message capture switched on."""
        ...

    async def get_cursors(self, chat_id: int, thread_id: int) -> dict[str, str]:
        """Return the (group, topic) per-resource poll cursor map."""
        ...

    async def set_cursors(
        self, chat_id: int, thread_id: int, cursors: dict[str, str], repo: str
    ) -> None:
        """Persist the per-resource cursor map iff still bound to ``repo``.

        The repo guard keeps an in-flight poll from stamping stale
        cursors onto a profile that was reset/rebound meanwhile.
        """
        ...

    async def migrate(self, old_chat_id: int, new_chat_id: int) -> None:
        """Re-key every topic of a group after a group→supergroup upgrade."""
        ...


class TokenVault(Protocol):
    """Stores group-supplied API tokens, encrypted at rest.

    Callers hand over and receive plaintext; encryption is the
    adapter's responsibility and ciphertext never leaves it.
    """

    async def store_token(self, chat_id: int, thread_id: int, token: str) -> None:
        """Encrypt and persist the (group, topic) token."""
        ...

    async def fetch_token(self, chat_id: int, thread_id: int) -> str | None:
        """Decrypt and return the (group, topic) token, if one is stored."""
        ...

    async def delete_token(self, chat_id: int, thread_id: int) -> None:
        """Discard the (group, topic) token."""
        ...


class RepoActions(Protocol):
    """On-demand actions against a group-bound repo with the group's token.

    Consumed by the interactive ``/issue`` and ``/repo`` commands; kept
    separate from :class:`RepoPoller` so neither consumer depends on the
    other's surface (ISP).
    """

    async def validate_token(self, repo: str, token: str) -> bool:
        """Whether the token can see the repo and write its issues."""
        ...

    async def open_issue(self, repo: str, token: str, title: str, body: str) -> str:
        """Create an issue in the bound repo; return its public URL."""
        ...

    async def open_summary(self, repo: str, token: str) -> RepoSummary:
        """Return a small open-items summary of the bound repo."""
        ...


class RepoPoller(Protocol):
    """Background polling of a group-bound repo's resource streams."""

    async def poll_resource(
        self, repo: str, token: str, resource: Resource, cursor: str | None
    ) -> tuple[list[RepoEvent], str]:
        """Return enriched events from one resource stream, plus a new cursor.

        The cursor is an ISO-8601 ``updated_at`` watermark. A falsy
        cursor baselines: the watermark advances to the newest item but
        no events are emitted, so enabling a rule never replays history.
        """
        ...


class IssueTracker(Protocol):
    """Files anonymous bug reports with the project's issue tracker."""

    async def open_issue(self, title: str, body: str) -> str:
        """Create an issue; return its public URL."""
        ...


class WikiPublisher(Protocol):
    """Writes discussions to a wiki talk page (spec section 9).

    Every log is one section: a single ``/log`` entry opens its own
    section, and a DM session holds one section for its whole exchange.
    """

    async def start_discussion(self, page: str, heading: str, text: str, summary: str) -> None:
        """Open a new section titled ``heading`` on ``page`` (always a new section)."""
        ...

    async def continue_discussion(self, page: str, heading: str, text: str, summary: str) -> None:
        """Append ``text`` inside the latest section titled ``heading``, creating it if absent."""
        ...

    async def upload_file(
        self, filename: str, content: bytes, content_type: str, summary: str, description: str
    ) -> str:
        """Upload a file and return the canonical wiki filename."""
        ...


class Sanitizer(Protocol):
    """Neutralizes user-supplied text before it may touch a wiki page (spec R7)."""

    def sanitize(self, text: str) -> str:
        """Return ``text`` with all structure-altering wikitext neutralized."""
        ...


class PseudonymFactory(Protocol):
    """Mints fresh random pseudonyms (spec R6: CSPRNG, never derived from user data)."""

    def mint(self) -> Pseudonym:
        """Return a new pseudonym, independent of any prior mint."""
        ...


class Clock(Protocol):
    """Injectable time source so session TTL logic is deterministic under test."""

    def now(self) -> datetime:
        """Return the current time as an aware UTC datetime."""
        ...
