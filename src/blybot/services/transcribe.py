"""Use-case: transcribe a DM session to Meta (spec R4, N2, N3).

**Layout:** the DM target is one talk page; each session is one section
on it (heading = pseudonym), so one section = one whole exchange. Every
message is a discussion line indented one level deeper than the last —
the wiki convention for a back-and-forth. Sections never interleave:
appends land inside the session's own section by heading, and the
publisher creates the section if it is missing.

**Write discipline (spec 10):** nothing is buffered persistently.
Messages are coalesced for at most ``debounce_seconds`` (N2) in memory,
then appended; a crash inside the window loses at most that burst, and
already-written content always survives restarts.

Known trade-off: indentation depth follows ``Session.message_count``,
which counts *received* messages. If a flush fails and its burst is
dropped (the accepted failure policy), the next published line skips the
lost depths — a visible seam that honestly reflects the gap.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from blybot.domain.ports import WikiWriteError
from blybot.domain.rendering import discussion_line, section_heading, timestamp
from blybot.observability import log_event

if TYPE_CHECKING:
    from blybot.domain.models import Session, TimestampGranularity
    from blybot.domain.ports import Sanitizer, WikiPublisher
    from blybot.services.sessions import SessionRegistry


@dataclass
class _Buffer:
    anchor: str
    heading: str
    target_page: str
    continuation: bool  # False until the session's section exists on the wiki
    lines: list[str] = field(default_factory=list)
    flusher: asyncio.Task[None] | None = None


@dataclass(eq=False)
class DmTranscriptionService:
    """Sanitizes and incrementally appends DM messages under a pseudonym."""

    publisher: WikiPublisher
    sanitizer: Sanitizer
    sessions: SessionRegistry
    target_page: str
    edit_summary: str
    debounce_seconds: float
    timestamp_granularity: TimestampGranularity
    _buffers: dict[int, _Buffer] = field(default_factory=dict)
    # Anchors whose section has been created on the wiki. Kept so the
    # first flush of a session *opens* its section instead of "continuing"
    # one — which could otherwise target a stale section from an old
    # session that happened to mint the same pseudonym. Entries are tiny
    # pseudonym strings (no identifiers) and are pruned on rollover.
    _published_anchors: set[tuple[str, str]] = field(default_factory=set)

    async def record(self, chat_id: int, text: str, target_page: str | None = None) -> Session:
        """Queue one DM for publication; return the session it belongs to.

        With a positive debounce the write happens shortly after the
        burst ends; failures inside that window are logged operationally
        (the content of a failed flush is dropped, never persisted).
        With debounce zero the write is immediate and failures propagate
        to the caller.
        """
        page = target_page or self.target_page
        session = self.sessions.advance(chat_id)
        line = discussion_line(
            session.message_count,
            self.sanitizer.sanitize(text),
            signature=session.pseudonym.value,
        )

        buffer = self._buffers.get(chat_id)
        if buffer is not None and (buffer.anchor != session.anchor or buffer.target_page != page):
            # The session rolled over mid-buffer; close out the old
            # identity's section before writing under the new one. A
            # failure here follows the debounced-failure policy (logged,
            # burst dropped) — it must not swallow the new message too.
            await self._flush_logged(chat_id)
            self._published_anchors.discard((buffer.target_page, buffer.anchor))
            buffer = None
        if buffer is None:
            buffer = _Buffer(
                anchor=session.anchor,
                heading=self.heading_for(session),
                target_page=page,
                continuation=(page, session.anchor) in self._published_anchors,
            )
            self._buffers[chat_id] = buffer
        buffer.lines.append(line)

        if self.debounce_seconds <= 0:
            await self._flush(chat_id)
        elif buffer.flusher is None or buffer.flusher.done():
            buffer.flusher = asyncio.get_running_loop().create_task(self._flush_later(chat_id))
        return session

    async def flush_all(self) -> None:
        """Flush every pending buffer immediately (graceful shutdown).

        ``_flush`` itself cancels each buffer's scheduled flusher.
        """
        for chat_id in list(self._buffers):
            await self._flush_logged(chat_id)

    def heading_for(self, session: Session) -> str:
        """The session's section heading: creation timestamp + pseudonym.

        Derived from immutable session fields, so every burst of the
        session reproduces the identical heading.
        """
        stamp = timestamp(session.created_at, self.timestamp_granularity)
        return section_heading(stamp, session.pseudonym.value)

    def page_for(self, session: Session, target_page: str | None = None) -> str:
        """Return the page (with section anchor) this session's discussion lands on.

        MediaWiki turns spaces into underscores in heading anchors; the
        heading charset (timestamp, colon, hyphen, pseudonym) contains
        nothing else needing escape.
        """
        page = target_page or self.target_page
        return f"{page}#{self.heading_for(session).replace(' ', '_')}"

    async def _flush_later(self, chat_id: int) -> None:
        await asyncio.sleep(self.debounce_seconds)
        await self._flush_logged(chat_id)

    async def _flush_logged(self, chat_id: int) -> None:
        """Flush a buffer, applying the debounced-failure policy (log, drop)."""
        try:
            await self._flush(chat_id)
        except WikiWriteError:
            log_event("dm_flush", "error")

    async def _flush(self, chat_id: int) -> None:
        buffer = self._buffers.pop(chat_id, None)
        if buffer is None or not buffer.lines:
            return
        flusher = buffer.flusher
        if flusher is not None and not flusher.done() and flusher is not asyncio.current_task():
            # A rollover or shutdown flushed this buffer early; the
            # scheduled flusher must not fire against the next buffer.
            flusher.cancel()
        write = (
            self.publisher.continue_discussion
            if buffer.continuation
            else self.publisher.start_discussion
        )
        await write(
            page=buffer.target_page,
            heading=buffer.heading,
            text="\n".join(buffer.lines),
            summary=self.edit_summary,
        )
        self._published_anchors.add((buffer.target_page, buffer.anchor))
        log_event("dm_flush", "ok", lines=len(buffer.lines))
