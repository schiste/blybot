"""Ports: the interfaces the domain and services depend on.

Adapters (Telegram, MediaWiki) implement these protocols; services depend
only on the protocols. This is the dependency-inversion seam of the
codebase — new transports or wiki clients plug in here without touching
business logic.
"""

from __future__ import annotations

from datetime import datetime
from typing import Protocol

from blybot.domain.models import Pseudonym


class WikiWriteError(Exception):
    """A wiki write failed after bounded retries.

    Defined in the domain so services can handle publish failures
    without importing the adapter that raised them.
    """


class IssueTrackerError(Exception):
    """Filing an issue with the tracker failed."""


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
