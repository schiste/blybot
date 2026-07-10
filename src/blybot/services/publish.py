"""Use-case: publish a ``/log``-marked group message to the Meta log page (spec R2).

Every ``/log`` opens its own section on the log talk page (one section =
one log), with the entry indented as a discussion line. The heading is
the coarse date — or a fixed neutral title when timestamps are disabled —
so nothing in the section identifies the author (R6).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Final

from blybot.domain.models import TimestampGranularity
from blybot.domain.rendering import discussion_line

if TYPE_CHECKING:
    from blybot.domain.ports import Clock, Sanitizer, WikiPublisher

UNDATED_HEADING: Final = "Log entry"


class NothingToPublishError(Exception):
    """Raised when the referenced message carries no publishable text (media-only)."""


@dataclass(frozen=True, slots=True)
class LogPublicationService:
    """Sanitizes one message and opens a section for it on the log page."""

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

        await self.publisher.start_discussion(
            page=self.target_page,
            heading=self._heading(),
            text=discussion_line(1, self.sanitizer.sanitize(raw_text)),
            summary=self.edit_summary,
        )

    def _heading(self) -> str:
        if self.timestamp_granularity is TimestampGranularity.DATE:
            return self.clock.now().strftime("%Y-%m-%d")
        return UNDATED_HEADING
