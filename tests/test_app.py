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
from telegram.error import TelegramError
from telegram.ext import Application, ChatMemberHandler, CommandHandler, MessageHandler

from blybot.adapters.telegram.admin import AdminHandlers
from blybot.adapters.telegram.app import (
    Lifecycle,
    Maintenance,
    build_application,
    repo_notify_loop,
    run_polling,
)
from blybot.domain.ports import StorageError
from blybot.observability import Counters
from blybot.services.binding import TokenBinding
from blybot.services.notify import RepoNotifier
from blybot.services.policy import GroupPolicy
from blybot.services.sessions import SessionRegistry
from tests.fakes import (
    FakeClock,
    FakePublisher,
    FakeRepoGateway,
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
    # log, start, flush, whoami, privacy, bug, issue x2, repo, help x2,
    # setup, setpage, setconsent, setrepo, events, revoke, settings, reset
    assert kinds.count(CommandHandler) == 19
    assert kinds.count(ChatMemberHandler) == 2  # greet-on-entry and newcomer
    assert kinds.count(MessageHandler) == 2  # migration and DM text


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


async def test_repo_notify_loop_delivers_digests_and_survives_send_failures() -> None:
    store = InMemoryProfiles()
    notifier = RepoNotifier(
        store=store,
        vault=store,
        gateway=FakeRepoGateway(),
        groups=GroupPolicy(allowed=set()),
        counters=Counters(),
    )

    sent: list[tuple[int, str, int | None]] = []

    class Recorder:
        async def send_message(
            self, chat_id: int, text: str, message_thread_id: int | None = None
        ) -> None:
            if chat_id == -13:
                raise TelegramError("kicked")
            sent.append((chat_id, text, message_thread_id))

    async def fake_collect() -> list[tuple[int, int, str]]:
        return [(-13, 0, "lost"), (-1, 7, "x/y:\n- Release")]

    notifier.collect = fake_collect  # type: ignore[method-assign]
    task = asyncio.ensure_future(repo_notify_loop(cast("Any", Recorder()), notifier, 0))
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
        vault=store,
        gateway=FakeRepoGateway(),
        groups=GroupPolicy(allowed=set()),
        counters=Counters(),
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
        vault=store,
        gateway=FakeRepoGateway(),
        groups=GroupPolicy(allowed=set()),
        counters=Counters(),
    )
    calls = {"n": 0}

    async def exploding_collect() -> list[tuple[int, int, str]]:
        calls["n"] += 1
        msg = "schema drift"
        raise RuntimeError(msg)

    notifier.collect = exploding_collect  # type: ignore[method-assign]
    task = asyncio.ensure_future(repo_notify_loop(cast("Any", None), notifier, 0))
    for _ in range(30):
        await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert calls["n"] >= 2  # it kept polling after the crash
