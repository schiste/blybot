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
import time
from dataclasses import dataclass, field
from datetime import timedelta
from typing import TYPE_CHECKING, Any, Final, Protocol

from telegram import Update
from telegram.error import RetryAfter, TelegramError, TimedOut
from telegram.ext import (
    AIORateLimiter,
    Application,
    ChatMemberHandler,
    CommandHandler,
    MessageHandler,
    filters,
)

from blybot.adapters.telegram._common import send_threaded
from blybot.domain.models import OutboundMessage
from blybot.domain.ports import StorageError
from blybot.observability import log_event

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from telegram import Bot

    from blybot.adapters.telegram.admin import AdminHandlers
    from blybot.adapters.telegram.analyze import AnalysisHandlers
    from blybot.adapters.telegram.capture import CaptureHandlers
    from blybot.adapters.telegram.handlers import GroupHandlers, PrivateHandlers
    from blybot.adapters.telegram.subscribe import SubscriptionHandlers
    from blybot.domain.ports import MessageArchive
    from blybot.observability import Counters
    from blybot.services.capture import CaptureService
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
                if self.archive is not None:
                    await _archive_heartbeat(self.archive)

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
        self._maintenance_task = loop.create_task(self.maintenance.run_forever())
        if self.notifier is not None:
            self._notify_task = loop.create_task(
                message_loop(app.bot, self.notifier, self.poll_interval_seconds, "repo_poll")
            )
        if self.scheduler is not None:
            self._actions_task = loop.create_task(
                message_loop(app.bot, self.scheduler, self.poll_interval_seconds, "action_tick")
            )
        if self.reminder is not None:
            self._reminder_task = loop.create_task(
                message_loop(app.bot, self.reminder, self.poll_interval_seconds, "capture_remind")
            )
        if self.subscription_scheduler is not None:
            self._sub_task = loop.create_task(
                message_loop(
                    app.bot, self.subscription_scheduler, self.poll_interval_seconds, "sub_tick"
                )
            )
        log_event("startup", "ok")

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


async def _archive_heartbeat(archive: MessageArchive) -> None:
    """Report archive growth (v3): rows only, never content."""
    try:
        log_event("archive_size", "ok", rows=await archive.total())
    except StorageError:
        log_event("archive_size", "error")


class MessageCollector(Protocol):
    """Anything the background tick can poll for outbound chat messages.

    Both the repo notifier and the action scheduler implement this —
    one delivery loop serves every collector (v3 phase 5 unification).
    """

    async def collect(self) -> list[OutboundMessage]:
        """Return this cycle's messages; called once per tick."""
        ...


_DELIVERY_MAX_RETRIES: Final = 3


async def _deliver(
    bot: Bot,
    message: OutboundMessage,
    label: str,
    sleep: Callable[[float], Awaitable[None]],
) -> None:
    """Send one message, honoring Telegram flood-control.

    ``RetryAfter`` (the bot is rate-limited) is waited out and retried;
    ``TimedOut`` is a transient network blip and retried after a short
    pause. Both are bounded by ``_DELIVERY_MAX_RETRIES`` — a scope that
    stays rate-limited past the budget is dropped like any other
    undeliverable message, never retried forever. Every other
    ``TelegramError`` (kicked, muted, chat-not-found) is permanent for
    this message and dropped at once. These three are *subclasses* of
    ``TelegramError``, so they must be caught before it.
    """
    reason = ""
    delay = 0.0
    for attempt in range(_DELIVERY_MAX_RETRIES + 1):
        try:
            await send_threaded(bot, message.chat_id, message.thread_id, message.text)
        except RetryAfter as exc:
            # retry_after is a timedelta under PTB_TIMEDELTA (an int on the
            # legacy path); honor the stated wait plus a 1s margin.
            wait = exc.retry_after
            seconds = wait.total_seconds() if isinstance(wait, timedelta) else wait
            reason, delay = "flood", seconds + 1
        except TimedOut:
            reason, delay = "timeout", 1.0
        except TelegramError:
            # Kicked from the group, muted, etc. — that scope's message is
            # lost, every other scope's still goes out.
            log_event(f"{label}_delivery", "ignored")
            return
        except Exception as exc:  # keep the delivery task alive
            # A non-Telegram error (a bug, an asyncio glitch) must not
            # escape and kill the whole background task — drop this one
            # message and carry on, mirroring the collect() guard. Log the
            # exception *type* only (privacy: never its message text).
            log_event(f"{label}_delivery", "error", error=type(exc).__name__)
            return
        else:
            return  # sent
        if attempt < _DELIVERY_MAX_RETRIES:
            await sleep(delay)
    # Still rate-limited/timing-out past the retry budget: drop like any
    # other undeliverable message rather than retry forever.
    log_event(f"{label}_delivery", "ignored", reason=reason)


def _next_deadline(previous: float, interval: float, now: float) -> float:
    """The next tick deadline: anchored to the schedule, missed ticks not replayed.

    When the work finished within the interval the cadence stays exactly
    on ``previous + interval`` (no drift); when it overran, the deadline
    resyncs to ``now + interval`` so a slow tick does not trigger a burst
    of catch-up ticks.
    """
    candidate = previous + interval
    return candidate if candidate > now else now + interval


async def message_loop(  # noqa: PLR0913 -- sleep/monotonic are injected test seams
    bot: Bot,
    collector: MessageCollector,
    interval_seconds: float,
    label: str,
    *,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    monotonic: Callable[[], float] = time.monotonic,
) -> None:
    """Poll ``collector`` and deliver its messages until cancelled.

    The cadence is anchored to a fixed deadline rather than sleeping a
    whole interval *after* the work, so a slow tick (e.g. a minutes-long
    analysis) does not push every later tick progressively late.
    """
    next_at = monotonic() + interval_seconds
    while True:
        await sleep(max(0.0, next_at - monotonic()))
        try:
            messages = await collector.collect()
        except Exception:
            log_event(label, "error")
            messages = []
        for message in messages:
            await _deliver(bot, message, label, sleep)
        next_at = _next_deadline(next_at, interval_seconds, monotonic())


def build_application(  # noqa: PLR0913 -- one handler bundle per concern
    token: str,
    group_handlers: GroupHandlers,
    private_handlers: PrivateHandlers,
    admin_handlers: AdminHandlers,
    lifecycle: Lifecycle,
    capture_handlers: CaptureHandlers | None = None,
    analysis_handlers: AnalysisHandlers | None = None,
    subscription_handlers: SubscriptionHandlers | None = None,
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


def run_polling(  # noqa: PLR0913 -- one handler bundle per concern
    token: str,
    group_handlers: GroupHandlers,
    private_handlers: PrivateHandlers,
    admin_handlers: AdminHandlers,
    lifecycle: Lifecycle,
    capture_handlers: CaptureHandlers | None = None,
    analysis_handlers: AnalysisHandlers | None = None,
    subscription_handlers: SubscriptionHandlers | None = None,
) -> None:
    """Poll until stopped; blocks for the process lifetime."""
    application = build_application(
        token,
        group_handlers,
        private_handlers,
        admin_handlers,
        lifecycle,
        capture_handlers,
        analysis_handlers,
        subscription_handlers,
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
    application.run_polling(allowed_updates=allowed)
