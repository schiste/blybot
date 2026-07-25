"""Use-case: file an anonymous bug report from chat.

The report is composed so it cannot carry side effects into the
tracker: the user text is indented as a literal code block, which
GitHub renders verbatim — no @-mention notifications, no markdown, no
links. No Telegram identifier is included anywhere (R6).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Final

from blybot.domain.models import (
    ActionSpec,
    OutboundMessage,
    StepSpec,
    TriggerKind,
    TriggerSpec,
)
from blybot.domain.ports import ActionError

if TYPE_CHECKING:
    from blybot.domain.models import ActionContext
    from blybot.domain.ports import IssueTracker

CONFIRMATION_TEMPLATE: Final = "Filed anonymously: {url}"

# The /bug pipeline: the handler provides the payload (report text), so
# there is no source step to run; the sink files and confirms.
BUG_ACTION: Final = ActionSpec(
    action_id="bug",
    trigger=TriggerSpec(kind=TriggerKind.COMMAND, command="bug"),
    source=StepSpec(name="provided"),
    transforms=(),
    sink=StepSpec(name="issue_tracker"),
)

_TITLE_LIMIT: Final = 64
_BODY_PREAMBLE: Final = (
    "Reported anonymously via the Telegram bot (`/bug`). No reporter identity is recorded.\n\n"
)


def issue_title(text: str) -> str:
    """Collapse text into a capped single-line issue title."""
    first_line = " ".join(text.split())
    if len(first_line) > _TITLE_LIMIT:
        first_line = first_line[: _TITLE_LIMIT - 1] + "…"
    return first_line


def as_code_block(text: str) -> str:
    """Indent text so GitHub renders it verbatim: no pings, no markdown."""
    return "\n".join(f"    {line}" for line in text.splitlines())


def compose_issue(text: str, preamble: str) -> tuple[str, str]:
    """Return the ``(title, body)`` for an anonymous issue from ``text``."""
    return issue_title(text), preamble + as_code_block(text)


@dataclass(frozen=True, slots=True)
class FeedbackService:
    """The ``issue_tracker`` sink: files an anonymous issue and confirms.

    Doubles as the direct-call service (:meth:`report`) and the action
    sink (:meth:`deliver`) — one composition, one tracker client.
    """

    tracker: IssueTracker

    async def report(self, text: str) -> str:
        """File ``text`` as an anonymous issue; return the issue URL."""
        title, body = compose_issue(text, _BODY_PREAMBLE)
        return await self.tracker.open_issue(title=title, body=body)

    async def deliver(self, context: ActionContext, payload: object) -> tuple[OutboundMessage, ...]:
        """File the payload text as an issue; confirm with its URL.

        The confirmation carries the caller's scope verbatim — for /bug
        that is a sentinel (0, 0): the handler routes the reply itself,
        so no private chat id ever enters the pipeline.
        """
        if not isinstance(payload, str) or not payload.strip():
            msg = "the issue tracker needs report text to file"
            raise ActionError(msg)
        url = await self.report(payload)
        confirmation = OutboundMessage(
            chat_id=context.scope.chat_id,
            thread_id=context.scope.thread_id,
            text=CONFIRMATION_TEMPLATE.format(url=url),
        )
        return (confirmation,)
