"""Use-case: publish a ``/log``-marked group message to the Meta log page (spec R2).

Every ``/log`` opens its own section on the log talk page (one section =
one log). The entry is signed with a **one-off pseudonym minted for
that single entry** — a pure label with zero linkage: it never repeats,
so it identifies nothing and nobody (R6). The heading is the
publication timestamp at the configured granularity plus that
pseudonym.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import timedelta
from typing import TYPE_CHECKING, Final

from blybot.domain.models import LogContent
from blybot.domain.rendering import discussion_line, file_link, section_heading, timestamp

if TYPE_CHECKING:
    from blybot.domain.models import TimestampGranularity
    from blybot.domain.ports import Clock, PseudonymFactory, Sanitizer, WikiPublisher

_EXTENSION_BY_CONTENT_TYPE: Final = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/gif": ".gif",
    "image/webp": ".webp",
}
_SAFE_FILENAME_PART: Final = re.compile(r"[^A-Za-z0-9]+")
_REVIEW_WINDOW = timedelta(days=7)


class NothingToPublishError(Exception):
    """Raised when the referenced message carries no supported publishable content."""


@dataclass(frozen=True, slots=True)
class PublishedMedia:
    """A file uploaded as part of one log entry."""

    filename: str
    review_deadline: str


@dataclass(frozen=True, slots=True)
class PublishedLog:
    """Result of publishing one log entry."""

    heading: str
    section_url: str | None = None
    media: tuple[PublishedMedia, ...] = ()


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

    async def publish(
        self, content: LogContent | str | None, target_page: str | None = None
    ) -> str:
        """Publish ``content`` anonymously; return the created section heading."""
        return (await self.publish_entry(content, target_page=target_page)).heading

    async def publish_entry(
        self,
        content: LogContent | str | None,
        target_page: str | None = None,
        page_url: str | None = None,
    ) -> PublishedLog:
        """Publish ``content`` anonymously; return section and uploaded file metadata.

        Callers pass only publishable message content — never the author.
        The signature is the anonymity boundary (spec R6). ``target_page``
        overrides the configured default (per-group pages, spec v2).
        Raises :class:`NothingToPublishError` when there is nothing to publish.
        """
        log_content = content if isinstance(content, LogContent) else LogContent(text=content)
        if not log_content.has_publishable_content:
            msg = "referenced message has no publishable content"
            raise NothingToPublishError(msg)

        stamp = timestamp(self.clock.now(), self.timestamp_granularity)
        entry_pseudonym = self.pseudonyms.mint()  # one-off, never reused
        heading = section_heading(stamp, entry_pseudonym.value)
        section_url = f"{page_url}#{heading.replace(' ', '_')}" if page_url else None
        deadline = (self.clock.now() + _REVIEW_WINDOW).date().isoformat()
        parts: list[str] = []
        uploaded_media: list[PublishedMedia] = []
        if log_content.text and log_content.text.strip():
            parts.append(self.sanitizer.sanitize(log_content.text))
        for index, media in enumerate(log_content.media, start=1):
            filename = _upload_filename(stamp, entry_pseudonym.value, index, media.content_type)
            uploaded = await self.publisher.upload_file(
                filename=filename,
                content=media.content,
                content_type=media.content_type,
                summary=self.edit_summary,
                description=_upload_description(
                    section_url=section_url,
                    upload_date=self.clock.now().date().isoformat(),
                    review_deadline=deadline,
                ),
            )
            uploaded_media.append(PublishedMedia(filename=uploaded, review_deadline=deadline))
            parts.append(file_link(uploaded))
        await self.publisher.start_discussion(
            page=target_page or self.target_page,
            heading=heading,
            text=discussion_line(1, "\n".join(parts), signature=entry_pseudonym.value),
            summary=self.edit_summary,
        )
        return PublishedLog(heading=heading, section_url=section_url, media=tuple(uploaded_media))


def _upload_filename(stamp: str | None, pseudonym: str, index: int, content_type: str) -> str:
    """Build a non-identifying wiki filename for one uploaded log attachment."""
    label = "_".join(part for part in ("Blybot", stamp, pseudonym, str(index)) if part)
    safe_label = _SAFE_FILENAME_PART.sub("_", label).strip("_") or "Blybot_upload"
    extension = _EXTENSION_BY_CONTENT_TYPE.get(content_type, ".bin")
    return f"{safe_label}{extension}"


def _upload_description(section_url: str | None, upload_date: str, review_deadline: str) -> str:
    """Build the file-page text for media uploaded from Telegram."""
    source = section_url or "Telegram message selected with /logmedia"
    return (
        "== Summary ==\n"
        "{{Information\n"
        "|description=Media attached to a Telegram message selected with /logmedia.\n"
        f"|source={source}\n"
        f"|date={upload_date}\n"
        "|author=Telegram author; Blybot does not store Telegram identities.\n"
        "|permission=Pending review by the Telegram author.\n"
        "}}\n\n"
        "== Licensing ==\n"
        "License status is pending Telegram author review. The reviewing author must add "
        "or confirm an appropriate free license before the deadline below.\n\n"
        "== Review deadline ==\n"
        f"Content must be checked by Telegram author before {review_deadline}; "
        "past that day media can be safely deleted."
    )
