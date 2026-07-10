"""Use-case: file an anonymous bug report from chat.

The report is composed so it cannot carry side effects into the
tracker: the user text is indented as a literal code block, which
GitHub renders verbatim — no @-mention notifications, no markdown, no
links. No Telegram identifier is included anywhere (R6).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Final

if TYPE_CHECKING:
    from blybot.domain.ports import IssueTracker

_TITLE_LIMIT: Final = 64
_BODY_PREAMBLE: Final = (
    "Reported anonymously via the Telegram bot (`/bug`). No reporter identity is recorded.\n\n"
)


def _title_from(text: str) -> str:
    first_line = " ".join(text.split())
    if len(first_line) > _TITLE_LIMIT:
        first_line = first_line[: _TITLE_LIMIT - 1] + "…"
    return first_line


def _as_code_block(text: str) -> str:
    return "\n".join(f"    {line}" for line in text.splitlines())


@dataclass(frozen=True, slots=True)
class FeedbackService:
    """Composes and files an anonymous issue; returns its URL."""

    tracker: IssueTracker

    async def report(self, text: str) -> str:
        """File ``text`` as an anonymous issue; return the issue URL."""
        return await self.tracker.open_issue(
            title=_title_from(text),
            body=_BODY_PREAMBLE + _as_code_block(text),
        )
