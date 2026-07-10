"""Telegram transport wiring (spec section 8).

Long polling via python-telegram-bot with privacy mode ON (R1): the
``allowed_updates`` list opts into exactly ``message``,
``my_chat_member`` and ``chat_member`` — reliable join detection via
``chat_member`` additionally requires the bot to be a group admin.

A maintenance task sweeps expired sessions and emits a periodic
heartbeat with the counter snapshot (spec 16); shutdown flushes pending
DM buffers so debounced content is not lost on a graceful restart.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any, Final

from telegram import Update
from telegram.ext import Application, ChatMemberHandler, CommandHandler, MessageHandler, filters

from blybot.observability import log_event

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from blybot.adapters.telegram.handlers import GroupHandlers, PrivateHandlers
    from blybot.observability import Counters
    from blybot.services.sessions import SessionRegistry
    from blybot.services.transcribe import DmTranscriptionService

MAINTENANCE_INTERVAL_SECONDS: Final = 60
HEARTBEAT_EVERY_TICKS: Final = 15  # one liveness line roughly every 15 minutes

_ALLOWED_UPDATES: Final = [Update.MESSAGE, Update.MY_CHAT_MEMBER, Update.CHAT_MEMBER]

_App = Application[Any, Any, Any, Any, Any, Any]


async def _maintenance_loop(sessions: SessionRegistry, counters: Counters) -> None:
    """Sweep expired sessions and prove liveness (spec 10, 16)."""
    ticks = 0
    while True:
        await asyncio.sleep(MAINTENANCE_INTERVAL_SECONDS)
        expired = sessions.sweep()
        if expired:
            counters.increment("sessions_expired", expired)
            log_event("session_sweep", "ok", expired=expired)
        ticks += 1
        if ticks % HEARTBEAT_EVERY_TICKS == 0:
            log_event("heartbeat", "ok", **counters.snapshot())


def run_polling(  # noqa: PLR0913 -- the composition root hands every collaborator over
    token: str,
    group_handlers: GroupHandlers,
    private_handlers: PrivateHandlers,
    sessions: SessionRegistry,
    transcription: DmTranscriptionService,
    counters: Counters,
    shutdown: Callable[[], Awaitable[None]],
) -> None:
    """Build the PTB application, register handlers, and poll until stopped."""

    async def post_init(app: _App) -> None:
        app.create_task(_maintenance_loop(sessions, counters))
        log_event("startup", "ok")

    async def post_shutdown(app: _App) -> None:
        del app
        await transcription.flush_all()
        await shutdown()
        log_event("shutdown", "ok")

    application = (
        Application.builder().token(token).post_init(post_init).post_shutdown(post_shutdown).build()
    )

    application.add_handler(CommandHandler("log", group_handlers.on_log))
    application.add_handler(CommandHandler("start", private_handlers.on_start))
    application.add_handler(
        ChatMemberHandler(group_handlers.on_my_chat_member, ChatMemberHandler.MY_CHAT_MEMBER)
    )
    application.add_handler(
        ChatMemberHandler(group_handlers.on_newcomer, ChatMemberHandler.CHAT_MEMBER)
    )
    application.add_handler(
        MessageHandler(filters.StatusUpdate.MIGRATE, group_handlers.on_migration)
    )
    application.add_handler(
        MessageHandler(
            filters.ChatType.PRIVATE & filters.TEXT & ~filters.COMMAND, private_handlers.on_dm
        )
    )

    application.run_polling(allowed_updates=_ALLOWED_UPDATES)
