"""Use-case: publish a ``/log``-marked group message to the Meta log page (spec R2).

Every ``/log`` opens its own section on the log talk page (one section =
one log). The entry is signed with a **one-off pseudonym minted for
that single entry** — a pure label with zero linkage: it never repeats,
so it identifies nothing and nobody (R6). The heading is the
publication timestamp at the configured granularity plus that
pseudonym.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from blybot.domain.rendering import discussion_line, section_heading, timestamp

if TYPE_CHECKING:
    from blybot.domain.models import TimestampGranularity
    from blybot.domain.ports import Clock, PseudonymFactory, Sanitizer, WikiPublisher


class NothingToPublishError(Exception):
    """Raised when the referenced message carries no publishable text (media-only)."""


@dataclass(frozen=True, slots=True)
class LogPublicationService:
    """Sanitizes one message and opens a section for it on the log page."""

    publisher: WikiPublisher
    sanitizer: Sanitizer
    pseudonyms: PseudonymFactory
    clock: Clock
    target_page: str
    edit_summary: str
    timestamp_granularity: TimestampGranularity

    async def publish(self, raw_text: str | None) -> str:
        """Publish ``raw_text`` anonymously; return the created section heading.

        Callers pass only the message *text* — never the author. The
        signature is the anonymity boundary (spec R6). Raises
        :class:`NothingToPublishError` when there is nothing to publish.
        """
        if raw_text is None or not raw_text.strip():
            msg = "referenced message has no text"
            raise NothingToPublishError(msg)

        stamp = timestamp(self.clock.now(), self.timestamp_granularity)
        entry_pseudonym = self.pseudonyms.mint()  # one-off, never reused
        heading = section_heading(stamp, entry_pseudonym.value)
        await self.publisher.start_discussion(
            page=self.target_page,
            heading=heading,
            text=discussion_line(
                1, self.sanitizer.sanitize(raw_text), signature=entry_pseudonym.value
            ),
            summary=self.edit_summary,
        )
        return heading
