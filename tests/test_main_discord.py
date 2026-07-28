"""Composition-root tests for the Discord platform selector (`PLATFORM=discord`)."""

from __future__ import annotations

import asyncio
import logging
from typing import Any, cast

import pytest
from cryptography.fernet import Fernet

import blybot.__main__ as entry
from blybot.adapters.discord.gateway import DiscordGatewayClient
from blybot.adapters.toolsdb.archive import ToolsDbArchive
from blybot.adapters.toolsdb.store import ToolsDbStore
from blybot.adapters.toolsdb.subscriptions import ToolsDbSubscriptions
from blybot.domain.ports import StorageError
from blybot.observability import Counters
from tests.test_config import REQUIRED


def _discord_env(monkeypatch: pytest.MonkeyPatch, **extra: str) -> None:
    for key, value in REQUIRED.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("PLATFORM", "discord")
    monkeypatch.setenv("DISCORD_BOT_TOKEN", "discord-secret")
    for key, value in extra.items():
        monkeypatch.setenv(key, value)


def _run(monkeypatch: pytest.MonkeyPatch, **extra: str) -> dict[str, Any]:
    """Route main() to the Discord path with the network start patched out."""
    _discord_env(monkeypatch, **extra)
    seen: dict[str, Any] = {}

    def fake_discord_run(client: DiscordGatewayClient, token: str, release: Any) -> None:
        seen.update(client=client, token=token, release=release)

    monkeypatch.setattr(entry, "discord_run", fake_discord_run)
    assert entry.main() == 0
    return seen


def _collectors(client: DiscordGatewayClient) -> dict[str, Any]:
    partial = cast("Any", client._on_setup)
    return {label: collector for collector, label in partial.keywords["collectors"]}


async def test_discord_full_deployment_wires_every_neutral_service(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    booted: list[str] = []
    monkeypatch.setattr(ToolsDbStore, "bootstrap", _recorder(booted, "profiles"))
    monkeypatch.setattr(ToolsDbArchive, "bootstrap", _recorder(booted, "messages"))
    monkeypatch.setattr(ToolsDbSubscriptions, "bootstrap", _recorder(booted, "subscriptions"))

    seen = _run(
        monkeypatch,
        PROFILE_ENCRYPTION_KEY=Fernet.generate_key().decode(),
        ARCHIVE_PSEUDONYM_KEY="long-random-operator-key",
    )
    client = cast("DiscordGatewayClient", seen["client"])
    assert seen["token"] == "discord-secret"  # noqa: S105 -- test fixture, not a secret

    gateway = client._gateway
    assert gateway.capture is not None  # capture ingestion is live
    assert gateway.masker is not None
    assert gateway.subscriptions is not None
    assert isinstance(gateway.directory.store, ToolsDbStore)

    collectors = _collectors(client)
    assert set(collectors) == {"sub_tick"}  # digests deliver; reminders off by default
    engine = collectors["sub_tick"].engine
    assert set(engine.sources) == {"archive_window"}
    assert set(engine.transforms) == {"prompt", "stats"}
    assert set(engine.sinks) == {"reply"}

    # The bootstrap closure covers all three stores.
    bootstrap = cast("Any", client._on_setup).keywords["bootstrap"]
    await bootstrap()
    assert booted == ["profiles", "messages", "subscriptions"]

    await seen["release"]()  # closes the LiftWing HTTP client


async def test_discord_store_only_deployment_has_no_capture(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    booted: list[str] = []
    monkeypatch.setattr(ToolsDbStore, "bootstrap", _recorder(booted, "profiles"))

    seen = _run(monkeypatch, PROFILE_ENCRYPTION_KEY=Fernet.generate_key().decode())
    client = cast("DiscordGatewayClient", seen["client"])
    gateway = client._gateway
    assert gateway.capture is None  # no pseudonym key: capture stays off
    assert gateway.subscriptions is None
    assert _collectors(client) == {}

    # bootstrap runs only the profile store (archive/subs were never built).
    await cast("Any", client._on_setup).keywords["bootstrap"]()
    assert booted == ["profiles"]
    await seen["release"]()  # no LiftWing client to close


async def test_discord_minimal_deployment_still_builds_the_gateway(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen = _run(monkeypatch)  # no encryption key at all
    client = cast("DiscordGatewayClient", seen["client"])
    gateway = client._gateway
    assert gateway.capture is None
    assert gateway.subscriptions is None
    assert gateway.directory.store is None
    assert cast("Any", client._on_setup).keywords["bootstrap"] is None
    await seen["release"]()


async def test_discord_reannounce_cadence_adds_the_reminder(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen = _run(
        monkeypatch,
        PROFILE_ENCRYPTION_KEY=Fernet.generate_key().decode(),
        ARCHIVE_PSEUDONYM_KEY="long-random-operator-key",
        CAPTURE_REANNOUNCE_DAYS="30",
    )
    client = cast("DiscordGatewayClient", seen["client"])
    collectors = _collectors(client)
    assert set(collectors) == {"sub_tick", "capture_remind"}
    assert collectors["capture_remind"].cadence.days == 30
    await seen["release"]()


def _recorder(sink: list[str], name: str) -> Any:
    async def boot(_self: object) -> None:
        sink.append(name)

    return boot


# --- the startup hook, background spawner, and network shell ------------------


def _startup_kwargs(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "bootstrap": None,
        "collectors": (),
        "poll_interval": 300,
        "counters": Counters(),
        "archive": None,
        "heartbeat_interval": 900.0,
    }
    base.update(overrides)
    return base


async def test_discord_startup_bootstraps_then_starts_the_delivery_loops(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    spawned: list[Any] = []

    def fake_spawn(coro: Any) -> None:
        spawned.append(coro)
        coro.close()  # never actually run the endless loop in the test

    monkeypatch.setattr(entry, "_spawn", fake_spawn)
    booted: list[int] = []

    async def bootstrap() -> None:
        booted.append(1)

    collectors = ((object(), "sub_tick"), (object(), "capture_remind"))
    with caplog.at_level(logging.INFO, logger="blybot"):
        await entry._discord_startup(
            cast("DiscordGatewayClient", object()),
            **_startup_kwargs(bootstrap=bootstrap, collectors=cast("Any", collectors)),
        )
    assert booted == [1]
    assert len(spawned) == 3  # one delivery loop per collector + the heartbeat
    assert any("event=startup outcome=ok" in m for m in caplog.messages)


async def test_discord_startup_without_a_bootstrap_still_starts_loops(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spawned: list[Any] = []

    def fake_spawn(coro: Any) -> None:
        spawned.append(coro)
        coro.close()

    monkeypatch.setattr(entry, "_spawn", fake_spawn)
    await entry._discord_startup(
        cast("DiscordGatewayClient", object()),
        **_startup_kwargs(collectors=cast("Any", ((object(), "sub_tick"),))),
    )
    assert len(spawned) == 2  # the delivery loop + the heartbeat


async def test_discord_startup_contains_a_bootstrap_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(entry, "_spawn", lambda coro: coro.close())

    async def bootstrap() -> None:
        raise StorageError

    # A storage outage is logged, not raised: the gateway still comes up.
    await entry._discord_startup(
        cast("DiscordGatewayClient", object()), **_startup_kwargs(bootstrap=bootstrap)
    )


class _StubArchive:
    def __init__(self, *, total: int | None = None) -> None:
        self._total = total

    async def total(self) -> int:
        if self._total is None:
            raise StorageError
        return self._total


def _sleep_then_cancel(monkeypatch: pytest.MonkeyPatch) -> None:
    """Let the heartbeat run exactly one body, then break its endless loop."""
    calls = {"n": 0}

    async def fake_sleep(_seconds: float) -> None:
        calls["n"] += 1
        if calls["n"] >= 2:
            raise asyncio.CancelledError

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)


async def test_discord_heartbeat_logs_liveness_and_archive_size(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    _sleep_then_cancel(monkeypatch)
    with caplog.at_level(logging.INFO, logger="blybot"), pytest.raises(asyncio.CancelledError):
        await entry._discord_heartbeat(Counters(), cast("Any", _StubArchive(total=7)), 900.0)
    assert any("event=heartbeat outcome=ok" in m for m in caplog.messages)
    assert any("event=archive_size outcome=ok rows=7" in m for m in caplog.messages)


async def test_discord_heartbeat_reports_an_archive_outage(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    _sleep_then_cancel(monkeypatch)
    with caplog.at_level(logging.INFO, logger="blybot"), pytest.raises(asyncio.CancelledError):
        await entry._discord_heartbeat(Counters(), cast("Any", _StubArchive(total=None)), 900.0)
    assert any("event=archive_size outcome=error" in m for m in caplog.messages)


async def test_discord_heartbeat_without_an_archive_still_beats(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    _sleep_then_cancel(monkeypatch)
    with caplog.at_level(logging.INFO, logger="blybot"), pytest.raises(asyncio.CancelledError):
        await entry._discord_heartbeat(Counters(), None, 900.0)
    assert any("event=heartbeat outcome=ok" in m for m in caplog.messages)
    assert not any("archive_size" in m for m in caplog.messages)


async def test_spawn_schedules_a_background_task() -> None:
    done: list[int] = []

    async def work() -> None:
        done.append(1)

    await entry._spawn(work())
    assert done == [1]


def test_discord_run_starts_the_client_then_releases() -> None:
    ran: list[str] = []
    released: list[int] = []

    class _FakeClient:
        def run(self, token: str) -> None:
            ran.append(token)

    async def release() -> None:
        released.append(1)

    entry.discord_run(cast("DiscordGatewayClient", _FakeClient()), "tok", release)
    assert ran == ["tok"]
    assert released == [1]
