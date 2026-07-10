"""Composition of talk-page discussion wikitext.

The markup added here (indentation colons, ``<br>``) is bot-supplied and
trusted; it is applied *after* user text has passed the sanitizer, which
is what keeps the composition safe.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from blybot.domain.models import TimestampGranularity

if TYPE_CHECKING:
    from datetime import datetime


def discussion_line(depth: int, text: str, signature: str | None = None) -> str:
    """Render one indented discussion line, optionally signed.

    ``depth`` is the message's 1-based ordinal in its exchange — each
    reply indents one level deeper, the wiki convention for a
    back-and-forth. Newlines inside the message become ``<br>`` so a
    multi-line message stays one discussion line and the indentation
    cannot be broken. ``signature`` (a pseudonym, never an identifier)
    is appended as ``--signature``; group log entries stay unsigned.
    """
    line = ":" * depth + " " + text.replace("\n", "<br>")
    if signature:
        line += f" --{signature}"
    return line


def timestamp(moment: datetime, granularity: TimestampGranularity) -> str | None:
    """Render the section-heading timestamp, or ``None`` when disabled."""
    if granularity is TimestampGranularity.MINUTE:
        return f"{moment:%Y-%m-%d} - {moment:%H:%M} UTC"
    if granularity is TimestampGranularity.DATE:
        return f"{moment:%Y-%m-%d}"
    return None


def section_heading(stamp: str | None, subject: str | None) -> str:
    """Compose ``"<stamp> : <subject>"``, tolerating either part missing."""
    if stamp and subject:
        return f"{stamp} : {subject}"
    return stamp or subject or "Log entry"
