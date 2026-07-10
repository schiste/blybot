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
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from blybot.domain.ports import WikiWriteError
from blybot.domain.rendering import discussion_line
from blybot.observability import log_event

if TYPE_CHECKING:
    from blybot.domain.models import Session
    from blybot.domain.ports import Sanitizer, WikiPublisher
    from blybot.services.sessions import SessionRegistry


@dataclass
class _Buffer:
    anchor: str
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
    _buffers: dict[int, _Buffer] = field(default_factory=dict)

    async def record(self, chat_id: int, text: str) -> Session:
        """Queue one DM for publication; return the session it belongs to.

        With a positive debounce the write happens shortly after the
        burst ends; failures inside that window are logged operationally
        (the content of a failed flush is dropped, never persisted).
        With debounce zero the write is immediate and failures propagate
        to the caller.
        """
        session = self.sessions.advance(chat_id)
        line = discussion_line(session.message_count, self.sanitizer.sanitize(text))

        buffer = self._buffers.get(chat_id)
        if buffer is not None and buffer.anchor != session.anchor:
            # The session rolled over mid-buffer; close out the old
            # identity's section before writing under the new one.
            await self._flush(chat_id)
            buffer = None
        if buffer is None:
            buffer = _Buffer(anchor=session.anchor)
            self._buffers[chat_id] = buffer
        buffer.lines.append(line)

        if self.debounce_seconds <= 0:
            await self._flush(chat_id)
        elif buffer.flusher is None or buffer.flusher.done():
            buffer.flusher = asyncio.get_running_loop().create_task(self._flush_later(chat_id))
        return session

    async def flush_all(self) -> None:
        """Flush every pending buffer immediately (graceful shutdown)."""
        for chat_id, buffer in list(self._buffers.items()):
            if buffer.flusher is not None and not buffer.flusher.done():
                buffer.flusher.cancel()
            try:
                await self._flush(chat_id)
            except WikiWriteError:
                log_event("dm_flush", "error")

    def page_for(self, session: Session) -> str:
        """Return the page (with section anchor) this session's discussion lands on."""
        return f"{self.target_page}#{session.anchor}"

    async def _flush_later(self, chat_id: int) -> None:
        await asyncio.sleep(self.debounce_seconds)
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
        await self.publisher.continue_discussion(
            page=self.target_page,
            heading=buffer.anchor,
            text="\n".join(buffer.lines),
            summary=self.edit_summary,
        )
        log_event("dm_flush", "ok", lines=len(buffer.lines))
