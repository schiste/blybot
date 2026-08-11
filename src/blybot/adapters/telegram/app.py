"""Telegram transport wiring (spec section 8).

Long polling via python-telegram-bot. The ``allowed_updates`` list opts
into ``message``, ``my_chat_member`` and ``chat_member`` — reliable join
detection via ``chat_member`` additionally requires the bot to be a
group admin. Capture-enabled deployments (v3) additionally subscribe to
``channel_post`` and run with privacy mode OFF; without capture, privacy
mode stays ON (R1) and group chatter is never even delivered.

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
from telegram.ext import (
    AIORateLimiter,
    Application,
    ChatMemberHandler,
    CommandHandler,
    MessageHandler,
    filters,
)

from blybot.adapters.telegram.transport import TELEGRAM_CAPABILITIES, TelegramTransport
from blybot.domain.ports import StorageError
from blybot.observability import log_event
from blybot.services.delivery import message_loop
from blybot.services.health import log_archive_size, log_heartbeat, log_startup

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from blybot.adapters.telegram.admin import AdminHandlers
    from blybot.adapters.telegram.analyze import AnalysisHandlers
    from blybot.adapters.telegram.capture import CaptureHandlers
    from blybot.adapters.telegram.handlers import GroupHandlers, PrivateHandlers
    from blybot.adapters.telegram.subscribe import SubscriptionHandlers
    from blybot.domain.models import PlatformCapabilities
    from blybot.domain.ports import MessageArchive
    from blybot.observability import Counters
    from blybot.services.capture import CaptureService
    from blybot.services.delivery import MessageCollector
    from blybot.services.notify import RepoNotifier
    from blybot.services.schedule import ActionScheduler
    from blybot.services.sessions import SessionRegistry
    from blybot.services.transcribe import DmTranscriptionService

_ALLOWED_UPDATES: Final = [Update.MESSAGE, Update.MY_CHAT_MEMBER, Update.CHAT_MEMBER]

_App = Application[Any, Any, Any, Any, Any, Any]


@dataclass(eq=False)
class Maintenance:
    """Periodic session sweep and liveness heartbeat (spec 10, 16)."""

    sessions: SessionRegistry
    counters: Counters
    # Capture deployments: the archive whose size each heartbeat reports.
    archive: MessageArchive | None = None
    # Capture deployments: pending consent revocations converge here, so
    # a quiet channel's disable becomes durable within one tick of a
    # storage recovery — independent of message arrival.
    capture: CaptureService | None = None
    interval_seconds: float = 60
    heartbeat_every_ticks: int = 15  # one liveness line roughly every 15 minutes

    async def run_forever(self) -> None:
        """Tick until cancelled (the polling process's whole lifetime)."""
        ticks = 0
        while True:
            await asyncio.sleep(self.interval_seconds)
            ticks += 1
            self.tick(ticks)
            if self.capture is not None:
                await self.capture.retry_denied()
            if ticks % self.heartbeat_every_ticks == 0:
                if self.capture is not None:
                    await self.capture.sweep_retention()
                await log_archive_size(self.archive)

    def tick(self, ticks: int) -> None:
        """Sweep expired sessions; prove liveness every Nth tick."""
        expired = self.sessions.sweep()
        if expired:
            self.counters.increment("sessions_expired", expired)
            log_event("session_sweep", "ok", expired=expired)
        if ticks % self.heartbeat_every_ticks == 0:
            log_heartbeat(self.counters)


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
    # Scheduled actions (capture-enabled deployments; v3 phase 4).
    scheduler: ActionScheduler | None = None
    # Periodic capture re-announcements (CAPTURE_REANNOUNCE_DAYS > 0).
    reminder: MessageCollector | None = None
    # DM digest subscriptions (capture-enabled deployments; §21).
    subscription_scheduler: MessageCollector | None = None
    poll_interval_seconds: float = 300
    _notify_task: asyncio.Task[None] | None = field(default=None, init=False)
    _actions_task: asyncio.Task[None] | None = field(default=None, init=False)
    _reminder_task: asyncio.Task[None] | None = field(default=None, init=False)
    _sub_task: asyncio.Task[None] | None = field(default=None, init=False)
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
        # The transport wraps the live bot; the neutral delivery loop only
        # ever sees this port, never python-telegram-bot.
        transport = TelegramTransport(app.bot)
        self._maintenance_task = loop.create_task(self.maintenance.run_forever())
        if self.notifier is not None:
            self._notify_task = loop.create_task(
                message_loop(transport, self.notifier, self.poll_interval_seconds, "repo_poll")
            )
        if self.scheduler is not None:
            self._actions_task = loop.create_task(
                message_loop(transport, self.scheduler, self.poll_interval_seconds, "action_tick")
            )
        if self.reminder is not None:
            self._reminder_task = loop.create_task(
                message_loop(transport, self.reminder, self.poll_interval_seconds, "capture_remind")
            )
        if self.subscription_scheduler is not None:
            self._sub_task = loop.create_task(
                message_loop(
                    transport, self.subscription_scheduler, self.poll_interval_seconds, "sub_tick"
                )
            )
        log_startup()

    async def post_shutdown(self, app: _App) -> None:
        """Stop maintenance, flush pending DM buffers, release the wiki client."""
        del app
        tasks = (
            self._maintenance_task,
            self._notify_task,
            self._actions_task,
            self._reminder_task,
            self._sub_task,
        )
        for task in tasks:
            if task is not None:
                task.cancel()
        for task in tasks:
            if task is not None:
                with contextlib.suppress(asyncio.CancelledError):
                    await task
        await self.transcription.flush_all()
        await self.release()
        log_event("shutdown", "ok")


def build_application(  # noqa: PLR0913, PLR0917 -- one handler bundle per concern
    token: str,
    group_handlers: GroupHandlers,
    private_handlers: PrivateHandlers,
    admin_handlers: AdminHandlers,
    lifecycle: Lifecycle,
    capture_handlers: CaptureHandlers | None = None,
    analysis_handlers: AnalysisHandlers | None = None,
    subscription_handlers: SubscriptionHandlers | None = None,
    capabilities: PlatformCapabilities = TELEGRAM_CAPABILITIES,
) -> _App:
    """Build the PTB application with every handler registered."""
    application = (
        Application.builder()
        .token(token)
        # Proactively pace every outgoing call under Telegram's ~30 msg/s
        # global and ~20 msg/min per-group ceilings. This is what keeps a
        # multi-scope digest/analysis fan-out from tripping flood control;
        # `_deliver` only has to mop up the rare RetryAfter that still leaks
        # through (max_retries=0 here so the two don't both retry).
        .rate_limiter(AIORateLimiter(max_retries=0))
        .post_init(lifecycle.post_init)
        .post_shutdown(lifecycle.post_shutdown)
        .build()
    )
    application.add_handler(CommandHandler("log", group_handlers.on_log))
    application.add_handler(CommandHandler("logmedia", group_handlers.on_logmedia))
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
        ("capture", admin_handlers.on_capture),
        ("llm", admin_handlers.on_llm),
        ("action", admin_handlers.on_action),
        ("subscribable", admin_handlers.on_subscribable),
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
    if capabilities.id_can_change:
        # Only platforms whose chat ids can migrate (Telegram's group ->
        # supergroup upgrade) need the re-key handler.
        application.add_handler(
            MessageHandler(filters.StatusUpdate.MIGRATE, group_handlers.on_migration)
        )
    application.add_handler(
        MessageHandler(
            filters.ChatType.PRIVATE & filters.StatusUpdate.CHAT_SHARED,
            private_handlers.on_chat_shared,
        )
    )
    application.add_handler(
        MessageHandler(
            filters.ChatType.PRIVATE & filters.TEXT & ~filters.COMMAND, private_handlers.on_dm
        )
    )
    if analysis_handlers is not None:
        for name, callback in (
            ("summarize", analysis_handlers.on_summarize),
            ("talkingpoints", analysis_handlers.on_talkingpoints),
            ("stats", analysis_handlers.on_stats),
            ("run", analysis_handlers.on_run),
        ):
            application.add_handler(CommandHandler(name, callback, filters=filters.ChatType.GROUPS))
    if subscription_handlers is not None:
        for name, callback in (
            ("subscribe", subscription_handlers.on_subscribe),
            ("unsubscribe", subscription_handlers.on_unsubscribe),
            ("mysubs", subscription_handlers.on_mysubs),
        ):
            application.add_handler(
                CommandHandler(name, callback, filters=filters.ChatType.PRIVATE)
            )
    if capture_handlers is not None:
        # Handler group 1: capture observes updates independently, so it
        # can never steal an update from (or be starved by) the
        # interactive handlers above, which all live in group 0.
        application.add_handler(
            MessageHandler(filters.UpdateType.CHANNEL_POST, capture_handlers.on_channel_post),
            group=1,
        )
        application.add_handler(
            MessageHandler(
                filters.ChatType.GROUPS & ~filters.COMMAND & ~filters.StatusUpdate.ALL,
                capture_handlers.on_group_message,
            ),
            group=1,
        )
        application.add_handler(
            ChatMemberHandler(capture_handlers.on_my_chat_member, ChatMemberHandler.MY_CHAT_MEMBER),
            group=1,
        )
    return application


def build_polling_application(  # noqa: PLR0913, PLR0917 -- one handler bundle per concern
    token: str,
    group_handlers: GroupHandlers,
    private_handlers: PrivateHandlers,
    admin_handlers: AdminHandlers,
    lifecycle: Lifecycle,
    capture_handlers: CaptureHandlers | None = None,
    analysis_handlers: AnalysisHandlers | None = None,
    subscription_handlers: SubscriptionHandlers | None = None,
    capabilities: PlatformCapabilities = TELEGRAM_CAPABILITIES,
) -> tuple[_App, list[str]]:
    """Wire the application and return it with the update types to request.

    Split out of :func:`run_polling` so the unified runtime (#78) can drive
    the same object graph through PTB's *async* entry points instead of
    ``run_polling``, which owns the event loop and so cannot share a
    process with another platform's client.
    """
    application = build_application(
        token,
        group_handlers,
        private_handlers,
        admin_handlers,
        lifecycle,
        capture_handlers,
        analysis_handlers,
        subscription_handlers,
        capabilities,
    )
    allowed = list(_ALLOWED_UPDATES)
    if analysis_handlers is not None:
        for name, callback in (
            ("summarize", analysis_handlers.on_summarize),
            ("talkingpoints", analysis_handlers.on_talkingpoints),
            ("stats", analysis_handlers.on_stats),
            ("run", analysis_handlers.on_run),
        ):
            application.add_handler(CommandHandler(name, callback, filters=filters.ChatType.GROUPS))
    if capture_handlers is not None:
        allowed.append(Update.CHANNEL_POST)
    return application, allowed


def run_polling(  # noqa: PLR0913, PLR0917 -- one handler bundle per concern
    token: str,
    group_handlers: GroupHandlers,
    private_handlers: PrivateHandlers,
    admin_handlers: AdminHandlers,
    lifecycle: Lifecycle,
    capture_handlers: CaptureHandlers | None = None,
    analysis_handlers: AnalysisHandlers | None = None,
    subscription_handlers: SubscriptionHandlers | None = None,
    capabilities: PlatformCapabilities = TELEGRAM_CAPABILITIES,
) -> None:
    """Poll until stopped; blocks for the process lifetime."""
    application, allowed = build_polling_application(
        token,
        group_handlers,
        private_handlers,
        admin_handlers,
        lifecycle,
        capture_handlers,
        analysis_handlers,
        subscription_handlers,
        capabilities,
    )
    application.run_polling(allowed_updates=allowed)


async def poll_until_cancelled(application: _App, allowed: list[str]) -> None:
    """Poll on the *caller's* event loop, until the task is cancelled.

    ``run_polling`` installs signal handlers and owns the loop, which two
    platforms in one process cannot both do. This drives the same
    lifecycle through PTB's async API so the unified runtime can gather it
    alongside the Discord client and the IRC session.
    """
    await application.initialize()
    await application.start()
    updater = application.updater
    if updater is None:  # pragma: no cover -- always built with one
        msg = "the application was built without an updater"
        raise RuntimeError(msg)
    await updater.start_polling(allowed_updates=allowed)
    try:
        await asyncio.Event().wait()  # cancelled by the runtime on shutdown
    finally:
        await updater.stop()
        await application.stop()
        await application.shutdown()
