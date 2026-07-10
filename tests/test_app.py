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
from telegram.ext import Application, ChatMemberHandler, CommandHandler, MessageHandler

from blybot.adapters.telegram.app import Lifecycle, Maintenance, build_application, run_polling
from blybot.observability import Counters
from blybot.services.sessions import SessionRegistry
from tests.fakes import FakeClock, FakePublisher, SequentialPseudonyms
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


def build() -> Application[Any, Any, Any, Any, Any, Any]:
    group_handlers, _, _ = make_group_handlers()
    private_handlers, _ = make_private_handlers()
    lifecycle, _, _ = make_lifecycle()
    return build_application(TOKEN, group_handlers, private_handlers, lifecycle)


def test_build_registers_every_handler() -> None:
    application = build()
    handlers = application.handlers[0]
    kinds = [type(handler) for handler in handlers]
    assert kinds.count(CommandHandler) == 7  # log, start, flush, whoami, privacy, help x2
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
    run_polling(TOKEN, group_handlers, private_handlers, lifecycle)

    assert seen["allowed_updates"] == [Update.MESSAGE, Update.MY_CHAT_MEMBER, Update.CHAT_MEMBER]


async def test_post_init_starts_the_maintenance_task() -> None:
    lifecycle, _, _ = make_lifecycle()
    created: list[Any] = []
    app = cast("_App", SimpleNamespace(create_task=created.append))

    await lifecycle.post_init(app)

    (coroutine,) = created
    assert coroutine.__qualname__ == "Maintenance.run_forever"
    coroutine.close()  # not scheduled in this test


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
