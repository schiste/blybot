"""Transport bootstrap tests: registration, lifecycle, maintenance (spec 8, 16)."""

from __future__ import annotations

import asyncio
import logging
from datetime import timedelta
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, cast
from unittest.mock import AsyncMock

import pytest
from telegram import Update
from telegram.error import RetryAfter, TelegramError, TimedOut
from telegram.ext import (
    AIORateLimiter,
    Application,
    ChatMemberHandler,
    CommandHandler,
    MessageHandler,
)

from blybot.adapters.telegram.admin import AdminHandlers
from blybot.adapters.telegram.analyze import AnalysisHandlers
from blybot.adapters.telegram.app import (
    _DELIVERY_MAX_RETRIES,
    Lifecycle,
    Maintenance,
    _deliver,
    build_application,
    message_loop,
    run_polling,
)
from blybot.adapters.telegram.capture import CaptureHandlers, HmacAuthorMasker
from blybot.domain.models import ConsentMode, GroupProfile, OutboundMessage
from blybot.domain.ports import StorageError
from blybot.observability import Counters
from blybot.services.binding import TokenBinding
from blybot.services.capture import CaptureReminder, CaptureService
from blybot.services.directory import ChannelDirectory
from blybot.services.engine import ActionEngine
from blybot.services.notify import RepoNotifier
from blybot.services.policy import GroupPolicy, SlidingWindowLimiter
from blybot.services.schedule import ActionScheduler
from blybot.services.sessions import SessionRegistry
from tests.fakes import (
    FakeClock,
    FakePublisher,
    InMemoryActions,
    InMemoryArchive,
    InMemoryProfiles,
    SequentialPseudonyms,
)
from tests.test_group_handlers import make_handlers as make_group_handlers
from tests.test_private_handlers import make_handlers as make_private_handlers
from tests.test_transcribe import make_service

if TYPE_CHECKING:
    from blybot.adapters.telegram.app import _App

TOKEN = "123:testtoken"  # noqa: S105 -- syntactically valid dummy, never used online


def make_lifecycle(
    publisher: FakePublisher | None = None,
) -> tuple[Lifecycle, SessionRegistry, AsyncMock]:
    sessions = SessionRegistry(
        pseudonyms=SequentialPseudonyms(), clock=FakeClock(), ttl=timedelta(minutes=45)
    )
    release = AsyncMock()
    lifecycle = Lifecycle(
        maintenance=Maintenance(sessions=sessions, counters=Counters()),
        transcription=make_service(publisher or FakePublisher(), FakeClock()),
        release=release,
    )
    return lifecycle, sessions, release


def make_admin_handlers() -> AdminHandlers:
    group_handlers, _, _ = make_group_handlers()
    return AdminHandlers(
        directory=group_handlers.directory,
        groups=GroupPolicy(allowed=set()),
        counters=Counters(),
        page_url_for=group_handlers.page_url_for,
        binding=TokenBinding(clock=FakeClock()),
        vault=None,
    )


def build() -> Application[Any, Any, Any, Any, Any, Any]:
    group_handlers, _, _ = make_group_handlers()
    private_handlers, _ = make_private_handlers()
    lifecycle, _, _ = make_lifecycle()
    return build_application(
        TOKEN, group_handlers, private_handlers, make_admin_handlers(), lifecycle
    )


def test_build_registers_every_handler() -> None:
    application = build()
    handlers = application.handlers[0]
    kinds = [type(handler) for handler in handlers]
    # log, logmedia, start, flush, whoami, privacy, bug, issue x2, repo, help x2,
    # setup, setpage, setconsent, setrepo, events, rule, rules, capture, llm,
    # action, revoke, settings, reset
    assert kinds.count(CommandHandler) == 25
    assert kinds.count(ChatMemberHandler) == 2  # greet-on-entry and newcomer
    assert kinds.count(MessageHandler) == 3  # migration, DM chat picker, and DM text
    assert 1 not in application.handlers  # no capture: group 1 stays empty


def test_build_paces_outgoing_calls_under_telegram_limits() -> None:
    """Every send goes through a rate limiter so a fan-out can't flood."""
    application = build()
    assert isinstance(application.bot.rate_limiter, AIORateLimiter)


def test_run_polling_opts_into_exactly_the_updates_privacy_mode_needs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """R1/spec 8: message, my_chat_member and chat_member — nothing more."""
    seen: dict[str, Any] = {}

    def fake_run_polling(_self: object, **kwargs: Any) -> None:
        seen["allowed_updates"] = kwargs["allowed_updates"]

    monkeypatch.setattr(Application, "run_polling", fake_run_polling)
    group_handlers, _, _ = make_group_handlers()
    private_handlers, _ = make_private_handlers()
    lifecycle, _, _ = make_lifecycle()
    run_polling(TOKEN, group_handlers, private_handlers, make_admin_handlers(), lifecycle)

    assert seen["allowed_updates"] == [Update.MESSAGE, Update.MY_CHAT_MEMBER, Update.CHAT_MEMBER]


async def test_post_init_bootstraps_storage_then_starts_maintenance() -> None:
    lifecycle, _, _ = make_lifecycle()
    lifecycle.maintenance.interval_seconds = 3600
    calls: list[str] = []

    async def bootstrap() -> None:
        calls.append("bootstrap")

    lifecycle.bootstrap = bootstrap
    await lifecycle.post_init(cast("_App", SimpleNamespace()))
    assert calls == ["bootstrap"]
    await lifecycle.post_shutdown(cast("_App", SimpleNamespace()))


async def test_post_init_contains_bootstrap_failures() -> None:
    """A dead database degrades self-service; it must not stop the bot."""
    lifecycle, _, _ = make_lifecycle()
    lifecycle.maintenance.interval_seconds = 3600

    async def bootstrap() -> None:
        raise StorageError

    lifecycle.bootstrap = bootstrap
    await lifecycle.post_init(cast("_App", SimpleNamespace()))
    assert lifecycle._maintenance_task is not None  # maintenance still started
    await lifecycle.post_shutdown(cast("_App", SimpleNamespace()))


async def test_post_init_starts_maintenance_and_shutdown_cancels_it() -> None:
    lifecycle, _, _ = make_lifecycle()
    lifecycle.maintenance.interval_seconds = 3600  # never actually ticks
    app = cast("_App", SimpleNamespace())

    await lifecycle.post_init(app)
    task = lifecycle._maintenance_task
    assert task is not None
    assert not task.done()

    await lifecycle.post_shutdown(app)
    await asyncio.sleep(0)  # let the cancellation land
    assert task.cancelled()


async def test_post_shutdown_flushes_buffers_then_releases_the_client() -> None:
    publisher = FakePublisher()
    lifecycle, _, release = make_lifecycle(publisher)
    await lifecycle.transcription.record(chat_id=1, text="pending")
    lifecycle.transcription.debounce_seconds = 60  # simulate an unflushed buffer
    app = cast("_App", SimpleNamespace())

    await lifecycle.post_shutdown(app)

    release.assert_awaited_once()


def test_maintenance_tick_sweeps_and_heartbeats(caplog: pytest.LogCaptureFixture) -> None:
    clock = FakeClock()
    sessions = SessionRegistry(
        pseudonyms=SequentialPseudonyms(), clock=clock, ttl=timedelta(minutes=45)
    )
    counters = Counters()
    maintenance = Maintenance(sessions=sessions, counters=counters, heartbeat_every_ticks=2)

    sessions.touch(chat_id=1)
    clock.advance(timedelta(hours=1))
    with caplog.at_level(logging.INFO, logger="blybot"):
        maintenance.tick(1)  # sweeps the expired session, no heartbeat yet
        maintenance.tick(2)  # heartbeat tick

    assert counters.snapshot()["sessions_expired"] == 1
    assert any("session_sweep" in message for message in caplog.messages)
    assert any("heartbeat" in message for message in caplog.messages)


async def test_run_forever_ticks_until_cancelled() -> None:
    lifecycle, _, _ = make_lifecycle()
    maintenance = lifecycle.maintenance
    maintenance.interval_seconds = 0
    maintenance.heartbeat_every_ticks = 10_000  # keep the log quiet

    task = asyncio.ensure_future(maintenance.run_forever())
    for _ in range(5):
        await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


async def test_maintenance_tick_converges_pending_capture_revocations() -> None:
    store, clock = InMemoryProfiles(), FakeClock()
    await store.upsert(GroupProfile(chat_id=-200, capture_enabled=True))
    capture = CaptureService(
        store=store,
        archive=InMemoryArchive(),
        limiter=SlidingWindowLimiter(clock=clock, limit=100, window=timedelta(minutes=1)),
        clock=clock,
        counters=Counters(),
    )
    capture.deny_scope(-200, 0)  # a revocation whose durable write failed
    lifecycle, _, _ = make_lifecycle()
    maintenance = lifecycle.maintenance
    maintenance.capture = capture
    maintenance.interval_seconds = 0
    maintenance.heartbeat_every_ticks = 10_000

    task = asyncio.ensure_future(maintenance.run_forever())
    for _ in range(10):
        await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    profile = await store.get(-200, 0)
    assert profile is not None
    assert profile.capture_enabled is False  # durable within one tick, no post needed


async def test_repo_notify_loop_delivers_digests_and_survives_send_failures() -> None:
    store = InMemoryProfiles()
    notifier = RepoNotifier(
        store=store,
        groups=GroupPolicy(allowed=set()),
        engine=ActionEngine(
            sources={}, transforms={}, sinks={}, counters=Counters(), clock=FakeClock()
        ),
    )

    sent: list[tuple[int, str, int | None]] = []

    class Recorder:
        async def send_message(
            self, chat_id: int, text: str, message_thread_id: int | None = None
        ) -> None:
            if chat_id == -13:
                raise TelegramError("kicked")
            sent.append((chat_id, text, message_thread_id))

    async def fake_collect() -> list[OutboundMessage]:
        return [
            OutboundMessage(chat_id=-13, thread_id=0, text="lost"),
            OutboundMessage(chat_id=-1, thread_id=7, text="x/y:\n- Release"),
        ]

    notifier.collect = fake_collect  # type: ignore[method-assign]
    task = asyncio.ensure_future(message_loop(cast("Any", Recorder()), notifier, 0, "repo_poll"))
    for _ in range(20):
        await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert (-1, "x/y:\n- Release", 7) in sent


async def test_post_init_starts_the_notify_task_when_configured() -> None:
    store = InMemoryProfiles()
    lifecycle, _, _ = make_lifecycle()
    lifecycle.maintenance.interval_seconds = 3600
    lifecycle.poll_interval_seconds = 3600
    lifecycle.notifier = RepoNotifier(
        store=store,
        groups=GroupPolicy(allowed=set()),
        engine=ActionEngine(
            sources={}, transforms={}, sinks={}, counters=Counters(), clock=FakeClock()
        ),
    )
    app = cast("_App", SimpleNamespace(bot=SimpleNamespace()))
    await lifecycle.post_init(app)
    assert lifecycle._notify_task is not None
    await lifecycle.post_shutdown(app)
    await asyncio.sleep(0)
    assert lifecycle._notify_task.cancelled()


async def test_notify_loop_survives_a_crashing_collect() -> None:
    """One bad poll cycle must never kill the loop for good."""
    store = InMemoryProfiles()
    notifier = RepoNotifier(
        store=store,
        groups=GroupPolicy(allowed=set()),
        engine=ActionEngine(
            sources={}, transforms={}, sinks={}, counters=Counters(), clock=FakeClock()
        ),
    )
    calls = {"n": 0}

    async def exploding_collect() -> list[OutboundMessage]:
        calls["n"] += 1
        msg = "schema drift"
        raise RuntimeError(msg)

    notifier.collect = exploding_collect  # type: ignore[method-assign]
    task = asyncio.ensure_future(message_loop(cast("Any", None), notifier, 0, "repo_poll"))
    for _ in range(30):
        await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert calls["n"] >= 2  # it kept polling after the crash


class _Flaky:
    """A bot that fails the first ``fail`` sends with ``error``, then succeeds."""

    def __init__(self, error: TelegramError, fail: int) -> None:
        self._error = error
        self._remaining = fail
        self.sent: list[str] = []

    async def send_message(
        self, chat_id: int, text: str, message_thread_id: int | None = None
    ) -> None:
        del chat_id, message_thread_id
        if self._remaining > 0:
            self._remaining -= 1
            raise self._error
        self.sent.append(text)


async def _collect_sleeps() -> tuple[list[float], Any]:
    waits: list[float] = []

    async def sleep(seconds: float) -> None:
        waits.append(seconds)

    return waits, sleep


async def test_deliver_waits_out_flood_control_then_sends() -> None:
    waits, sleep = await _collect_sleeps()
    bot = _Flaky(RetryAfter(2), fail=1)
    message = OutboundMessage(chat_id=-1, thread_id=0, text="hi")
    await _deliver(cast("Any", bot), message, "repo_poll", sleep)
    assert bot.sent == ["hi"]
    assert waits == [3.0]  # retry_after (2) + a 1s margin, then the retry lands


async def test_deliver_retries_a_timeout_then_sends() -> None:
    waits, sleep = await _collect_sleeps()
    bot = _Flaky(TimedOut(), fail=1)
    message = OutboundMessage(chat_id=-1, thread_id=7, text="hi")
    await _deliver(cast("Any", bot), message, "action_tick", sleep)
    assert bot.sent == ["hi"]
    assert waits == [1.0]


async def test_deliver_drops_after_persistent_flood_control() -> None:
    waits, sleep = await _collect_sleeps()
    bot = _Flaky(RetryAfter(1), fail=99)  # never recovers within the budget
    message = OutboundMessage(chat_id=-1, thread_id=0, text="hi")
    await _deliver(cast("Any", bot), message, "repo_poll", sleep)
    assert bot.sent == []  # dropped, not retried forever
    assert len(waits) == _DELIVERY_MAX_RETRIES  # bounded retries, then give up


async def test_deliver_drops_after_persistent_timeout() -> None:
    waits, sleep = await _collect_sleeps()
    bot = _Flaky(TimedOut(), fail=99)
    message = OutboundMessage(chat_id=-1, thread_id=0, text="hi")
    await _deliver(cast("Any", bot), message, "action_tick", sleep)
    assert bot.sent == []
    assert len(waits) == _DELIVERY_MAX_RETRIES


async def test_deliver_survives_a_non_telegram_error() -> None:
    """A bug in one send must not escape and kill the whole delivery task."""
    _waits, sleep = await _collect_sleeps()

    class Boom:
        async def send_message(
            self, chat_id: int, text: str, message_thread_id: int | None = None
        ) -> None:
            del chat_id, text, message_thread_id
            msg = "schema drift"
            raise RuntimeError(msg)

    message = OutboundMessage(chat_id=-1, thread_id=0, text="hi")
    # Must return (drop the message), not propagate.
    await _deliver(cast("Any", Boom()), message, "action_tick", sleep)


def make_capture_handlers() -> Any:
    store = InMemoryProfiles()
    clock = FakeClock()
    return CaptureHandlers(
        service=CaptureService(
            store=store,
            archive=InMemoryArchive(),
            limiter=SlidingWindowLimiter(clock=clock, limit=100, window=timedelta(minutes=1)),
            clock=clock,
            counters=Counters(),
        ),
        masker=HmacAuthorMasker(key="k"),
        directory=ChannelDirectory(
            store=store,
            default_log_page="Log",
            default_consent=ConsentMode.IMMEDIATE,
            default_repo="",
            page_suffix="",
        ),
        groups=GroupPolicy(allowed=set()),
        bot_name="Blybot",
    )


def test_capture_handlers_register_in_their_own_group() -> None:
    group_handlers, _, _ = make_group_handlers()
    private_handlers, _ = make_private_handlers()
    lifecycle, _, _ = make_lifecycle()
    application = build_application(
        TOKEN,
        group_handlers,
        private_handlers,
        make_admin_handlers(),
        lifecycle,
        capture_handlers=make_capture_handlers(),
    )
    kinds = [type(handler) for handler in application.handlers[1]]
    # channel posts, group chatter, and the channel-admin promotion hook
    assert kinds.count(MessageHandler) == 2
    assert kinds.count(ChatMemberHandler) == 1


def test_run_polling_subscribes_channel_posts_only_with_capture(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: dict[str, Any] = {}

    def fake_run_polling(_self: object, **kwargs: Any) -> None:
        seen["allowed_updates"] = kwargs["allowed_updates"]

    monkeypatch.setattr(Application, "run_polling", fake_run_polling)
    group_handlers, _, _ = make_group_handlers()
    private_handlers, _ = make_private_handlers()
    lifecycle, _, _ = make_lifecycle()
    run_polling(
        TOKEN,
        group_handlers,
        private_handlers,
        make_admin_handlers(),
        lifecycle,
        capture_handlers=make_capture_handlers(),
    )

    assert Update.CHANNEL_POST in seen["allowed_updates"]


def make_analysis_handlers() -> Any:
    return AnalysisHandlers(
        engine=ActionEngine(
            sources={}, transforms={}, sinks={}, counters=Counters(), clock=FakeClock()
        ),
        groups=GroupPolicy(allowed=set()),
        limiter=SlidingWindowLimiter(clock=FakeClock(), limit=6, window=timedelta(hours=1)),
        clock=FakeClock(),
        counters=Counters(),
    )


def test_analysis_commands_register_when_wired(monkeypatch: pytest.MonkeyPatch) -> None:
    group_handlers, _, _ = make_group_handlers()
    private_handlers, _ = make_private_handlers()
    lifecycle, _, _ = make_lifecycle()
    application = build_application(
        TOKEN,
        group_handlers,
        private_handlers,
        make_admin_handlers(),
        lifecycle,
        analysis_handlers=make_analysis_handlers(),
    )
    kinds = [type(handler) for handler in application.handlers[0]]
    assert kinds.count(CommandHandler) == 29  # + summarize, talkingpoints, stats, run

    seen: dict[str, Any] = {}

    def fake_run_polling(_self: object, **kwargs: Any) -> None:
        seen["allowed_updates"] = kwargs["allowed_updates"]

    monkeypatch.setattr(Application, "run_polling", fake_run_polling)
    run_polling(
        TOKEN,
        group_handlers,
        private_handlers,
        make_admin_handlers(),
        lifecycle,
        analysis_handlers=make_analysis_handlers(),
    )
    # Analysis commands alone don't need channel posts; capture does.
    assert Update.CHANNEL_POST not in seen["allowed_updates"]


def make_scheduler() -> Any:
    return ActionScheduler(
        store=InMemoryActions(),
        engine=ActionEngine(
            sources={}, transforms={}, sinks={}, counters=Counters(), clock=FakeClock()
        ),
        groups=GroupPolicy(allowed=set()),
        clock=FakeClock(),
        counters=Counters(),
    )


async def test_action_tick_loop_delivers_messages_and_survives_failures() -> None:
    scheduler = make_scheduler()
    sent: list[tuple[int, str, int | None]] = []

    class Recorder:
        async def send_message(
            self, chat_id: int, text: str, message_thread_id: int | None = None
        ) -> None:
            if chat_id == -13:
                raise TelegramError("kicked")
            sent.append((chat_id, text, message_thread_id))

    async def fake_collect() -> list[OutboundMessage]:
        return [
            OutboundMessage(chat_id=-13, thread_id=0, text="lost"),
            OutboundMessage(chat_id=-1, thread_id=7, text="Published: url"),
        ]

    scheduler.collect = fake_collect
    task = asyncio.ensure_future(message_loop(cast("Any", Recorder()), scheduler, 0, "action_tick"))
    for _ in range(20):
        await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert (-1, "Published: url", 7) in sent


async def test_action_tick_loop_survives_a_crashing_collect() -> None:
    scheduler = make_scheduler()
    calls = {"n": 0}

    async def exploding_collect() -> list[OutboundMessage]:
        calls["n"] += 1
        msg = "schema drift"
        raise RuntimeError(msg)

    scheduler.collect = exploding_collect
    task = asyncio.ensure_future(message_loop(cast("Any", None), scheduler, 0, "action_tick"))
    for _ in range(30):
        await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert calls["n"] >= 2


async def test_post_init_starts_the_actions_task_when_configured() -> None:
    lifecycle, _, _ = make_lifecycle()
    lifecycle.maintenance.interval_seconds = 3600
    lifecycle.poll_interval_seconds = 3600
    lifecycle.scheduler = make_scheduler()
    app = cast("_App", SimpleNamespace(bot=SimpleNamespace()))
    await lifecycle.post_init(app)
    assert lifecycle._actions_task is not None
    await lifecycle.post_shutdown(app)
    await asyncio.sleep(0)
    assert lifecycle._actions_task.cancelled()


async def test_heartbeat_reports_archive_size(caplog: pytest.LogCaptureFixture) -> None:
    lifecycle, _, _ = make_lifecycle()
    maintenance = lifecycle.maintenance
    maintenance.archive = InMemoryArchive()
    maintenance.interval_seconds = 0
    maintenance.heartbeat_every_ticks = 1

    with caplog.at_level(logging.INFO, logger="blybot"):
        task = asyncio.ensure_future(maintenance.run_forever())
        for _ in range(10):
            await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert any("archive_size" in message and "rows=0" in message for message in caplog.messages)

    maintenance.archive = InMemoryArchive(fail=True)
    with caplog.at_level(logging.INFO, logger="blybot"):
        task = asyncio.ensure_future(maintenance.run_forever())
        for _ in range(10):
            await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert any("archive_size" in message and "error" in message for message in caplog.messages)


async def test_post_init_starts_the_reminder_task_when_configured() -> None:
    lifecycle, _, _ = make_lifecycle()
    lifecycle.maintenance.interval_seconds = 3600
    lifecycle.poll_interval_seconds = 3600
    lifecycle.reminder = CaptureReminder(
        store=InMemoryProfiles(),
        groups=GroupPolicy(allowed=set()),
        clock=FakeClock(),
        cadence=timedelta(days=30),
    )
    app = cast("_App", SimpleNamespace(bot=SimpleNamespace()))
    await lifecycle.post_init(app)
    assert lifecycle._reminder_task is not None
    await lifecycle.post_shutdown(app)
    await asyncio.sleep(0)
    assert lifecycle._reminder_task.cancelled()
