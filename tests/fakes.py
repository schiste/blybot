"""In-memory fakes implementing the domain ports for unit tests."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from blybot.domain.models import Pseudonym
from blybot.domain.ports import WikiWriteError


@dataclass
class FakePublisher:
    """Records discussion writes instead of hitting the network.

    Each recorded entry is ``(page, heading, text, summary)``; ``started``
    holds new sections, ``continued`` holds appends into existing ones.
    """

    started: list[tuple[str, str, str, str]] = field(default_factory=list)
    continued: list[tuple[str, str, str, str]] = field(default_factory=list)

    async def start_discussion(self, page: str, heading: str, text: str, summary: str) -> None:
        self.started.append((page, heading, text, summary))

    async def continue_discussion(self, page: str, heading: str, text: str, summary: str) -> None:
        self.continued.append((page, heading, text, summary))


class FailingPublisher:
    """Publisher whose every write fails (as if retries were exhausted)."""

    async def start_discussion(self, page: str, heading: str, text: str, summary: str) -> None:
        del page, heading, text, summary
        raise WikiWriteError

    async def continue_discussion(self, page: str, heading: str, text: str, summary: str) -> None:
        del page, heading, text, summary
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
class SequentialPseudonyms:
    """Deterministic pseudonym factory: Anon-1, Anon-2, ..."""

    counter: int = 0

    def mint(self) -> Pseudonym:
        self.counter += 1
        return Pseudonym(f"Anon-{self.counter}")
