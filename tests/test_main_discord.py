"""Composition-root tests for the Discord platform selector (`PLATFORM=discord`)."""

from __future__ import annotations

import logging
import os
from typing import Any, cast

import pytest
from cryptography.fernet import Fernet

import blybot.__main__ as entry
from blybot.adapters.discord.gateway import DiscordGatewayClient
from blybot.adapters.toolsdb.archive import ToolsDbArchive
from blybot.adapters.toolsdb.store import ToolsDbStore
from blybot.adapters.toolsdb.subscriptions import ToolsDbSubscriptions
from blybot.config import load_config
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
    """Build the Discord graph without ever connecting.

    The build/start split (#78) is what makes this possible without owning
    an event loop: `start()` is a coroutine the caller decides to run.
    """
    _discord_env(monkeypatch, **extra)
    runtime = entry.build_discord(load_config())
    started = runtime.start()
    started.close()  # never actually dial Discord
    client = cast("DiscordGatewayClient", runtime.transport.client)  # type: ignore[union-attr]
    return {
        "client": client,
        "token": os.environ["DISCORD_BOT_TOKEN"],
        "release": runtime.release,
        "transport": runtime.transport,
    }


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
    assert gateway.analysis is not None  # on-demand analyses are wired
    assert isinstance(gateway.directory.store, ToolsDbStore)

    collectors = _collectors(client)
    # Digests deliver and repo notifications poll; reminders are off by default.
    assert set(collectors) == {"sub_tick", "action_tick", "repo_notify", "session_sweep"}
    engine = collectors["sub_tick"].engine
    assert collectors["repo_notify"].engine is engine
    assert (
        collectors["action_tick"].engine is engine
    )  # scheduled analyses too  # one engine for the deployment
    assert set(engine.sources) == {"archive_window", "repo_events"}
    assert set(engine.transforms) == {"prompt", "stats", "rule_match", "log_publish"}
    # The wiki_section sink lets the on-demand analyses publish to the wiki,
    # exactly like Telegram; the reply sink stays for scheduled digest DMs and
    # chat_message carries the rule-matched repo events.
    assert set(engine.sinks) == {
        "wiki_section",
        "reply",
        "subscriber_dm",
        "chat_message",
        "chat_confirm",
    }
    assert gateway.analysis.engine is engine  # analyses run through this engine

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
    assert gateway.analysis is None  # analyses need the archive too
    # Repo notifications need only the profile store, so they still run here.
    collectors = _collectors(client)
    # session_sweep runs on every tier: DM transcription works without a store,
    # so expired sessions must be evicted regardless (see SessionSweeper).
    assert set(collectors) == {"repo_notify", "session_sweep"}
    engine = collectors["repo_notify"].engine
    assert set(engine.sources) == {"repo_events"}
    # log_publish/chat_confirm are on every tier: /log needs neither store nor
    # archive, it publishes the message it is handed.
    assert set(engine.transforms) == {"rule_match", "log_publish"}
    assert set(engine.sinks) == {"chat_message", "chat_confirm"}

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
    assert set(collectors) == {
        "sub_tick",
        "action_tick",
        "repo_notify",
        "session_sweep",
        "capture_remind",
    }
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


# The heartbeat/startup assertions moved to tests/test_health.py when the
# logging became a shared neutral helper (#46); what stays Discord's own
# concern is that the loop is actually spawned, covered by the startup tests.


async def test_spawn_schedules_a_background_task() -> None:
    done: list[int] = []

    async def work() -> None:
        done.append(1)

    await entry._spawn(work())
    assert done == [1]


def test_main_dispatches_to_the_selected_platform(monkeypatch: pytest.MonkeyPatch) -> None:
    """`run_discord` still owns its own loop; only the split moved (#78)."""
    _discord_env(monkeypatch)
    ran: list[str] = []
    released: list[int] = []

    async def start() -> None:
        ran.append("started")

    async def release() -> None:
        released.append(1)

    monkeypatch.setattr(
        entry,
        "build_discord",
        lambda _config: entry.PlatformRuntime(start=start, release=release, platform="discord"),
    )
    assert entry.main() == 0
    assert (ran, released) == (["started"], [1])
