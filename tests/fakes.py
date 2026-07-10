"""In-memory fakes implementing the domain ports for unit tests."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from blybot.domain.models import Pseudonym


@dataclass
class FakePublisher:
    """Records appends instead of hitting the network."""

    appends: list[tuple[str, str, str]] = field(default_factory=list)

    async def append(self, page: str, text: str, summary: str) -> None:
        self.appends.append((page, text, summary))


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
