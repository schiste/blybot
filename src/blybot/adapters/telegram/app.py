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
import contextlib
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Final

from telegram import Update
from telegram.error import TelegramError
from telegram.ext import Application, ChatMemberHandler, CommandHandler, MessageHandler, filters

from blybot.adapters.telegram._common import send_threaded
from blybot.domain.ports import StorageError
from blybot.observability import log_event

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from telegram import Bot

    from blybot.adapters.telegram.admin import AdminHandlers
    from blybot.adapters.telegram.handlers import GroupHandlers, PrivateHandlers
    from blybot.observability import Counters
    from blybot.services.notify import RepoNotifier
    from blybot.services.sessions import SessionRegistry
    from blybot.services.transcribe import DmTranscriptionService

_ALLOWED_UPDATES: Final = [Update.MESSAGE, Update.MY_CHAT_MEMBER, Update.CHAT_MEMBER]

_App = Application[Any, Any, Any, Any, Any, Any]


@dataclass(eq=False)
class Maintenance:
    """Periodic session sweep and liveness heartbeat (spec 10, 16)."""

    sessions: SessionRegistry
    counters: Counters
    interval_seconds: float = 60
    heartbeat_every_ticks: int = 15  # one liveness line roughly every 15 minutes

    async def run_forever(self) -> None:
        """Tick until cancelled (the polling process's whole lifetime)."""
        ticks = 0
        while True:
            await asyncio.sleep(self.interval_seconds)
            ticks += 1
            self.tick(ticks)

    def tick(self, ticks: int) -> None:
        """Sweep expired sessions; prove liveness every Nth tick."""
        expired = self.sessions.sweep()
        if expired:
            self.counters.increment("sessions_expired", expired)
            log_event("session_sweep", "ok", expired=expired)
        if ticks % self.heartbeat_every_ticks == 0:
            log_event("heartbeat", "ok", **self.counters.snapshot())


@dataclass(eq=False)
class Lifecycle:
    """Startup and graceful-shutdown hooks for the polling application."""

    maintenance: Maintenance
    transcription: DmTranscriptionService
    release: Callable[[], Awaitable[None]]
    # Storage schema bootstrap (self-service deployments only). A failure
    # is contained: self-service degrades to defaults, the bot still runs.
    bootstrap: Callable[[], Awaitable[None]] | None = None
    # Repo-event digests (self-service deployments with events on).
    notifier: RepoNotifier | None = None
    poll_interval_seconds: float = 300
    _notify_task: asyncio.Task[None] | None = field(default=None, init=False)
    # Scheduled directly on the loop (PTB's create_task pre-start warns and
    # would not track it anyway); held here so shutdown can cancel it.
    _maintenance_task: asyncio.Task[None] | None = field(default=None, init=False)

    async def post_init(self, app: _App) -> None:
        """Bootstrap storage, then start the maintenance and poll tasks."""
        if self.bootstrap is not None:
            try:
                await self.bootstrap()
            except StorageError:
                log_event("storage_bootstrap", "error")
        loop = asyncio.get_running_loop()
        self._maintenance_task = loop.create_task(self.maintenance.run_forever())
        if self.notifier is not None:
            self._notify_task = loop.create_task(
                repo_notify_loop(app.bot, self.notifier, self.poll_interval_seconds)
            )
        log_event("startup", "ok")

    async def post_shutdown(self, app: _App) -> None:
        """Stop maintenance, flush pending DM buffers, release the wiki client."""
        del app
        for task in (self._maintenance_task, self._notify_task):
            if task is not None:
                task.cancel()
        for task in (self._maintenance_task, self._notify_task):
            if task is not None:
                with contextlib.suppress(asyncio.CancelledError):
                    await task
        await self.transcription.flush_all()
        await self.release()
        log_event("shutdown", "ok")


async def repo_notify_loop(bot: Bot, notifier: RepoNotifier, interval_seconds: float) -> None:
    """Poll bound repositories and deliver digests until cancelled."""
    while True:
        await asyncio.sleep(interval_seconds)
        try:
            digests = await notifier.collect()
        except Exception:
            log_event("repo_poll", "error")
            continue
        for chat_id, thread_id, digest in digests:
            try:
                await send_threaded(bot, chat_id, thread_id, digest)
            except TelegramError:
                # Kicked from the group, muted, etc. — that group's
                # digest is lost, everyone else's still goes out.
                log_event("repo_digest", "ignored")


def build_application(
    token: str,
    group_handlers: GroupHandlers,
    private_handlers: PrivateHandlers,
    admin_handlers: AdminHandlers,
    lifecycle: Lifecycle,
) -> _App:
    """Build the PTB application with every handler registered."""
    application = (
        Application.builder()
        .token(token)
        .post_init(lifecycle.post_init)
        .post_shutdown(lifecycle.post_shutdown)
        .build()
    )
    application.add_handler(CommandHandler("log", group_handlers.on_log))
    application.add_handler(CommandHandler("start", private_handlers.on_start))
    application.add_handler(CommandHandler("flush", private_handlers.on_flush))
    application.add_handler(CommandHandler("whoami", private_handlers.on_whoami))
    application.add_handler(CommandHandler("privacy", private_handlers.on_privacy))
    application.add_handler(
        CommandHandler("bug", private_handlers.on_bug, filters=filters.ChatType.PRIVATE)
    )
    application.add_handler(
        CommandHandler("issue", private_handlers.on_bug, filters=filters.ChatType.PRIVATE)
    )
    application.add_handler(
        CommandHandler("issue", group_handlers.on_issue, filters=filters.ChatType.GROUPS)
    )
    application.add_handler(
        CommandHandler("repo", group_handlers.on_repo, filters=filters.ChatType.GROUPS)
    )
    for name, callback in (
        ("setup", admin_handlers.on_setup),
        ("setpage", admin_handlers.on_setpage),
        ("setconsent", admin_handlers.on_setconsent),
        ("setrepo", admin_handlers.on_setrepo),
        ("events", admin_handlers.on_events),
        ("rule", admin_handlers.on_rule),
        ("rules", admin_handlers.on_rules),
        ("revoke", admin_handlers.on_revoke),
        ("settings", admin_handlers.on_settings),
        ("reset", admin_handlers.on_reset),
    ):
        application.add_handler(CommandHandler(name, callback, filters=filters.ChatType.GROUPS))
    application.add_handler(
        CommandHandler("help", private_handlers.on_help, filters=filters.ChatType.PRIVATE)
    )
    application.add_handler(
        CommandHandler("help", group_handlers.on_help, filters=filters.ChatType.GROUPS)
    )
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
    return application


def run_polling(
    token: str,
    group_handlers: GroupHandlers,
    private_handlers: PrivateHandlers,
    admin_handlers: AdminHandlers,
    lifecycle: Lifecycle,
) -> None:
    """Poll until stopped; blocks for the process lifetime."""
    application = build_application(
        token, group_handlers, private_handlers, admin_handlers, lifecycle
    )
    application.run_polling(allowed_updates=_ALLOWED_UPDATES)
