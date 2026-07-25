"""On-demand analysis commands: /summarize, /talkingpoints, /stats, /run.

Each command builds a one-shot action (same recipes and parameters as
``/action add``) and runs it through the engine immediately. Commands
are admin-gated (analyses cost inference calls and publish publicly)
and throttled per chat. The bot acknowledges before running — a chunked
analysis can take minutes — and the pipeline's confirmation message
(with the published page URL) lands when it finishes.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Final

from blybot.adapters.telegram._common import GROUP_TYPES, send_threaded, thread_of
from blybot.adapters.telegram.admin import REPLY_NOT_ADMIN, is_group_admin
from blybot.domain.models import ActionScope
from blybot.domain.ports import ActionError
from blybot.observability import log_event
from blybot.services.actions import ActionParseError, command_action

if TYPE_CHECKING:
    from telegram import Update
    from telegram.ext import ContextTypes

    from blybot.domain.ports import Clock
    from blybot.observability import Counters
    from blybot.services.engine import ActionEngine
    from blybot.services.policy import GroupPolicy, SlidingWindowLimiter

REPLY_THROTTLED: Final = "Too many analyses recently — try again later."
REPLY_WORKING: Final = (
    "Analyzing… a large window can take a few minutes. I'll post the result here."
)
REPLY_FAILED: Final = "The analysis failed and nothing was published — try again later."
REPLY_EMPTY: Final = "Nothing to analyze: the archive has no messages in that window."
REPLY_RUN_USAGE: Final = "Usage: /run <template> [24h|7d] [key=value …]"


@dataclass(eq=False)
class AnalysisHandlers:
    """Group command handlers driving one-shot analysis pipelines."""

    engine: ActionEngine
    groups: GroupPolicy
    limiter: SlidingWindowLimiter
    clock: Clock
    counters: Counters

    async def on_summarize(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Summarize the scope's recent archive onto its wiki page."""
        await self._run_command(update, context, "summarize", "summarize")

    async def on_talkingpoints(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Extract talking points from the scope's recent archive."""
        await self._run_command(update, context, "talkingpoints", "talking_points")

    async def on_stats(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Publish activity statistics for the scope's recent archive."""
        await self._run_command(update, context, "stats", "stats")

    async def on_run(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Run any named prompt template on demand."""
        args = list(context.args or ())
        if not args:
            await self._maybe_reply(update, context, REPLY_RUN_USAGE)
            return
        await self._run_command(update, context, "run", f"prompt:{args[0]}", arg_tokens=args[1:])

    async def _run_command(
        self,
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
        command: str,
        recipe: str,
        arg_tokens: list[str] | None = None,
    ) -> None:
        gated = await self._admin_scope(update, context)
        if gated is None:
            return
        chat_id, thread_id = gated
        if not self.limiter.allow("analysis", chat_id):
            await send_threaded(context.bot, chat_id, thread_id, REPLY_THROTTLED)
            return
        tokens = arg_tokens if arg_tokens is not None else list(context.args or ())
        try:
            spec = command_action(command, recipe, tokens)
        except ActionParseError as error:
            await send_threaded(context.bot, chat_id, thread_id, str(error))
            return
        await send_threaded(context.bot, chat_id, thread_id, REPLY_WORKING)
        scope = ActionScope(chat_id=chat_id, thread_id=thread_id)
        try:
            messages = await self.engine.run(scope, spec, self.clock.now())
        except ActionError as error:
            await send_threaded(context.bot, chat_id, thread_id, str(error))
            return
        except Exception:
            # Adapter failures (wiki write, storage) after retries: tell
            # the admin plainly, log operationally. Deliberately broad —
            # a command handler must never let an exception escape.
            self.counters.increment("analyses_failed")
            log_event("analysis_command", "error")
            await send_threaded(context.bot, chat_id, thread_id, REPLY_FAILED)
            return
        if not messages:
            await send_threaded(context.bot, chat_id, thread_id, REPLY_EMPTY)
            return
        for message in messages:
            await send_threaded(context.bot, message.chat_id, message.thread_id, message.text)

    async def _admin_scope(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> tuple[int, int] | None:
        chat = update.effective_chat
        message = update.effective_message
        if chat is None or message is None or chat.type not in GROUP_TYPES:
            return None
        if not self.groups.is_allowed(chat.id):
            return None
        thread_id = thread_of(update)
        user = message.from_user
        if user is None or not await is_group_admin(context.bot, chat.id, user.id):
            await send_threaded(context.bot, chat.id, thread_id, REPLY_NOT_ADMIN)
            return None
        return chat.id, thread_id

    async def _maybe_reply(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE, text: str
    ) -> None:
        gated = await self._admin_scope(update, context)
        if gated is not None:
            await send_threaded(context.bot, gated[0], gated[1], text)
