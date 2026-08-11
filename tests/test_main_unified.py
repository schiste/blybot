"""The unified runtime: every configured platform in one process (#78).

Bridging needs the process that *receives* a message to reach the platform
that must *send* it. A separate bridging job cannot do that — Telegram's
getUpdates is exclusive to one poller and a second IRC connection collides
on the nick — so a mirror requires one process hosting every adapter.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from cryptography.fernet import Fernet

import blybot.__main__ as entry
from blybot.adapters.telegram.transport import TELEGRAM_CAPABILITIES
from blybot.config import ConfigurationError, load_config
from tests.test_config import REQUIRED


class _FakeTransport:
    capabilities = TELEGRAM_CAPABILITIES

    async def send(self, message: object) -> None:  # pragma: no cover -- never sent to
        raise AssertionError(message)


def _never_run(monkeypatch: pytest.MonkeyPatch) -> None:
    """Build the graph but never actually start a loop."""
    monkeypatch.setattr(asyncio, "run", lambda coro: coro.close())


def _unified_env(monkeypatch: pytest.MonkeyPatch, **extra: str) -> None:
    for key, value in REQUIRED.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("PLATFORM", "unified")
    for key, value in extra.items():
        monkeypatch.setenv(key, value)


def _stub_builders(monkeypatch: pytest.MonkeyPatch, built: dict[str, Any]) -> None:
    """Replace each platform builder with one that records its router."""

    def make(name: str) -> Any:
        def build(_config: Any, bridge: Any = None) -> entry.PlatformRuntime:
            built[name] = bridge

            async def start() -> None:
                await asyncio.Event().wait()  # runs until cancelled

            # Mirrors the real builders: IRC's transport needs a live
            # socket, so it registers itself later from `_irc_main`.
            transport = None if name == "irc" else _FakeTransport()
            return entry.PlatformRuntime(start=start, transport=transport, platform=name)

        return build

    for name in ("telegram", "discord", "irc"):
        monkeypatch.setattr(entry, f"build_{name}", make(name))


def test_only_the_configured_platforms_are_built(monkeypatch: pytest.MonkeyPatch) -> None:
    """Unified mode runs what this config has credentials for, not all three."""
    _unified_env(monkeypatch, IRC_SERVER="irc.libera.chat")  # telegram token is in REQUIRED
    built: dict[str, Any] = {}
    _stub_builders(monkeypatch, built)
    _never_run(monkeypatch)

    assert entry.main() == 0

    assert sorted(built) == ["irc", "telegram"]  # no Discord token, no Discord


def test_every_platform_shares_one_router(monkeypatch: pytest.MonkeyPatch) -> None:
    """A relay from one platform has to reach the others' transports."""
    _unified_env(
        monkeypatch,
        IRC_SERVER="irc.libera.chat",
        DISCORD_BOT_TOKEN="dc",  # noqa: S106 -- test fixture, not a secret
    )
    built: dict[str, Any] = {}
    _stub_builders(monkeypatch, built)
    _never_run(monkeypatch)

    assert entry.main() == 0

    routers = list(built.values())
    assert len(routers) == 3
    assert all(router is routers[0] for router in routers)
    # ...and each platform whose transport exists at build time is reachable.
    assert sorted(routers[0].transports) == ["discord", "telegram"]


def test_unified_mode_needs_at_least_one_platform(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Otherwise the process starts, mirrors nothing, and looks healthy."""
    for key, value in REQUIRED.items():
        monkeypatch.setenv(key, value)
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN")
    monkeypatch.setenv("PLATFORM", "unified")

    assert entry.main() == 2
    assert "at least one platform" in capsys.readouterr().err


def test_unified_config_needs_no_single_platform_key() -> None:
    """The per-platform requirement is relaxed; `run_unified` checks instead."""
    env = dict(REQUIRED) | {"PLATFORM": "unified"}
    del env["TELEGRAM_BOT_TOKEN"]
    assert load_config(env).platform == "unified"


def test_an_unknown_platform_still_names_unified_among_the_options() -> None:
    env = dict(REQUIRED) | {"PLATFORM": "slack"}
    with pytest.raises(ConfigurationError, match="unified"):
        load_config(env)


def test_the_router_reads_membership_from_the_store_when_configured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _unified_env(monkeypatch, PROFILE_ENCRYPTION_KEY=Fernet.generate_key().decode())
    built: dict[str, Any] = {}
    _stub_builders(monkeypatch, built)
    _never_run(monkeypatch)

    assert entry.main() == 0

    assert built["telegram"].store is not None
    # The echo fence needs to know what the bot is called on each platform.
    assert set(built["telegram"].bridge.bot_authors) == {"irc", "telegram", "discord"}


def test_without_a_store_the_router_simply_resolves_no_targets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _unified_env(monkeypatch)
    built: dict[str, Any] = {}
    _stub_builders(monkeypatch, built)
    _never_run(monkeypatch)

    assert entry.main() == 0
    assert built["telegram"].store is None


async def test_one_platform_ending_stops_the_others_and_releases() -> None:
    """A hole in the mirror is worse than stopping: the survivors would
    keep relaying to each other while silently dropping the rest."""
    released: list[str] = []
    cancelled: list[str] = []

    def runtime(name: str, *, ends: bool) -> entry.PlatformRuntime:
        async def start() -> None:
            if ends:
                return
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cancelled.append(name)
                raise

        async def release() -> None:
            released.append(name)

        return entry.PlatformRuntime(start=start, release=release, platform=name)

    await entry._run_together(
        [runtime("telegram", ends=False), runtime("irc", ends=True)],
    )

    assert cancelled == ["telegram"]
    assert sorted(released) == ["irc", "telegram"]  # every client closed


async def test_a_platform_crashing_surfaces_rather_than_being_swallowed() -> None:
    """A silent exit would look like a clean shutdown to the job runner."""

    async def boom() -> None:
        msg = "the gateway died"
        raise RuntimeError(msg)

    async def forever() -> None:
        await asyncio.Event().wait()

    with pytest.raises(RuntimeError, match="the gateway died"):
        await entry._run_together(
            [
                entry.PlatformRuntime(start=forever, platform="telegram"),
                entry.PlatformRuntime(start=boom, platform="irc"),
            ]
        )
