"""Ports: the interfaces the domain and services depend on.

Adapters (Telegram, MediaWiki) implement these protocols; services depend
only on the protocols. This is the dependency-inversion seam of the
codebase — new transports or wiki clients plug in here without touching
business logic.
"""

from __future__ import annotations

from datetime import datetime
from typing import Protocol

from blybot.domain.models import GroupProfile, Pseudonym, RepoEvent, RepoSummary, Resource


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


class ProfileStore(Protocol):
    """Persists per-group self-service profiles (spec 11: ToolsDB)."""

    async def get(self, chat_id: int, thread_id: int) -> GroupProfile | None:
        """Return the (group, topic) profile, or ``None`` if unconfigured."""
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
