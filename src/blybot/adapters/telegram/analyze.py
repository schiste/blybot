"""On-demand analysis commands: /summarize, /talkingpoints, /stats, /run.

Each command runs a one-shot analysis (same recipes and parameters as
``/action add``) through the neutral
:class:`~blybot.services.analysis_run.AnalysisService` and publishes the
result to the scope's wiki page. These handlers keep only the Telegram
bits — resolving the admin scope, sending the "analysing…" progress
pre-message, and routing the final reply into the forum topic; the run
logic (admit → throttle → parse → run → outcome) lives once in the
service (issue #41, contract-first). Commands are admin-gated (analyses
cost inference calls and publish publicly) and throttled per chat.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Final

from blybot.adapters.telegram._common import (
    GROUP_TYPES,
    scope_of,
    send_threaded,
    telegram_target,
    thread_of,
)
from blybot.adapters.telegram.admin import REPLY_NOT_ADMIN, is_group_admin

if TYPE_CHECKING:
    from telegram import Update
    from telegram.ext import ContextTypes

    from blybot.domain.models import Scope
    from blybot.services.analysis_run import AnalysisService
    from blybot.services.policy import GroupPolicy

REPLY_WORKING: Final = (
    "Analyzing… a large window can take a few minutes. I'll post the result here."
)
REPLY_RUN_USAGE: Final = "Usage: /run <template> [24h|7d] [key=value …]"


@dataclass(eq=False)
class AnalysisHandlers:
    """Group command handlers driving one-shot analysis pipelines."""

    analysis: AnalysisService
    groups: GroupPolicy

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
        scope = await self._admin_scope(update, context)
        if scope is None:
            return
        chat_id, thread_id = telegram_target(scope)
        thread_id = thread_id or 0
        tokens = arg_tokens if arg_tokens is not None else list(context.args or ())

        async def announce() -> None:
            # The "analysing…" pre-message lands only when the run is
            # committed to — the service calls this after the throttle and
            # a clean parse, so a refusal or parse error never precedes it.
            await send_threaded(context.bot, chat_id, thread_id, REPLY_WORKING)

        # The admin scope resolved above, so the caller is an admin here.
        result = await self.analysis.run_analysis(
            scope,
            is_admin=True,
            command=command,
            recipe=recipe,
            tokens=tokens,
            on_started=announce,
        )
        await send_threaded(context.bot, chat_id, thread_id, result.text)

    async def _admin_scope(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> Scope | None:
        chat = update.effective_chat
        message = update.effective_message
        if chat is None or message is None or chat.type not in GROUP_TYPES:
            return None
        if not self.groups.is_allowed(scope_of(update)):
            return None
        thread_id = thread_of(update)
        user = message.from_user
        if user is None or not await is_group_admin(context.bot, chat.id, user.id):
            await send_threaded(context.bot, chat.id, thread_id, REPLY_NOT_ADMIN)
            return None
        return scope_of(update)

    async def _maybe_reply(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE, text: str
    ) -> None:
        scope = await self._admin_scope(update, context)
        if scope is not None:
            chat_id, thread_id = telegram_target(scope)
            await send_threaded(context.bot, chat_id, thread_id or 0, text)
