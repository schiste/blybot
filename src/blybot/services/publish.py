"""Use-case: publish a ``/log``-marked group message to the Meta log page (spec R2)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from blybot.domain.models import LogEntry, TimestampGranularity

if TYPE_CHECKING:
    from blybot.domain.ports import Clock, Sanitizer, WikiPublisher


class NothingToPublishError(Exception):
    """Raised when the referenced message carries no publishable text (media-only)."""


@dataclass(frozen=True, slots=True)
class LogPublicationService:
    """Sanitizes and appends one anonymous entry to the configured log page."""

    publisher: WikiPublisher
    sanitizer: Sanitizer
    clock: Clock
    target_page: str
    edit_summary: str
    timestamp_granularity: TimestampGranularity

    async def publish(self, raw_text: str | None) -> None:
        """Publish ``raw_text`` anonymously; raise if there is nothing to publish.

        Callers pass only the message *text* — never the author. The
        signature is the anonymity boundary (spec R6).
        """
        if raw_text is None or not raw_text.strip():
            msg = "referenced message has no text"
            raise NothingToPublishError(msg)

        entry = LogEntry(text=self.sanitizer.sanitize(raw_text))
        await self.publisher.append(
            page=self.target_page,
            text=self._render(entry),
            summary=self.edit_summary,
        )

    def _render(self, entry: LogEntry) -> str:
        if self.timestamp_granularity is TimestampGranularity.DATE:
            stamp = self.clock.now().strftime("%Y-%m-%d")
            return f"\n* ({stamp}) {entry.text}"
        return f"\n* {entry.text}"
