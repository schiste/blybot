"""Composition-root tests for the IRC platform selector (`PLATFORM=irc`)."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any, cast

import pytest
from cryptography.fernet import Fernet

import blybot.__main__ as entry
from blybot.adapters.irc.capabilities import IRC_CAPABILITIES
from blybot.adapters.toolsdb.archive import ToolsDbArchive
from blybot.adapters.toolsdb.store import ToolsDbStore
from blybot.domain.ports import StorageError
from tests.test_config import REQUIRED

# Captured before `_run` patches it out, so the shell can still be exercised.
_REAL_IRC_MAIN = entry._irc_main

if TYPE_CHECKING:
    from blybot.adapters.irc.gateway import IrcGateway
    from blybot.config import Config


def _irc_env(monkeypatch: pytest.MonkeyPatch, **extra: str) -> None:
    for key, value in REQUIRED.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("PLATFORM", "irc")
    monkeypatch.setenv("IRC_SERVER", "irc.libera.chat")
    monkeypatch.setenv("IRC_CHANNELS", "#wikipedia-fr")
    for key, value in extra.items():
        monkeypatch.setenv(key, value)


def _run(monkeypatch: pytest.MonkeyPatch, **extra: str) -> dict[str, Any]:
    """Route main() to the IRC path with the network dial patched out."""
    _irc_env(monkeypatch, **extra)
    seen: dict[str, Any] = {}

    def fake_main(config: Config, gateway: IrcGateway, store: Any, archive: Any) -> Any:
        seen.update(config=config, gateway=gateway, store=store, archive=archive)
        return _noop()

    monkeypatch.setattr(entry, "_irc_main", fake_main)
    # `run_irc` owns the event loop; the caller may already be inside one.
    monkeypatch.setattr(asyncio, "run", lambda coro: coro.close())
    assert entry.main() == 0
    return seen


async def _noop() -> None:
    return None  # pragma: no cover -- closed unstarted by the patched asyncio.run


def test_irc_full_deployment_wires_capture_and_the_masker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen = _run(
        monkeypatch,
        PROFILE_ENCRYPTION_KEY=Fernet.generate_key().decode(),
        ARCHIVE_PSEUDONYM_KEY="long-random-operator-key",
    )
    gateway = cast("IrcGateway", seen["gateway"])
    assert gateway.capture is not None  # capture ingestion is live
    assert gateway.masker is not None
    assert isinstance(seen["store"], ToolsDbStore)
    assert isinstance(seen["archive"], ToolsDbArchive)
    assert gateway.commands.capabilities is IRC_CAPABILITIES
    # The capture chunk cap comes from the platform, not a literal in services.
    assert gateway.capture.max_chars == IRC_CAPABILITIES.max_message_chars


def test_irc_store_only_deployment_has_no_capture(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen = _run(monkeypatch, PROFILE_ENCRYPTION_KEY=Fernet.generate_key().decode())
    gateway = cast("IrcGateway", seen["gateway"])
    assert gateway.capture is None  # no pseudonym key: capture stays off
    assert gateway.masker is None
    assert seen["archive"] is None


def test_irc_minimal_deployment_still_builds_the_gateway(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen = _run(monkeypatch)  # no encryption key at all
    gateway = cast("IrcGateway", seen["gateway"])
    assert gateway.capture is None
    assert gateway.directory.store is None
    assert seen["store"] is None


class _Writer:
    """A stream writer that records bytes instead of sending them."""

    def __init__(self) -> None:
        self.written: list[bytes] = []
        self.closed = False

    def write(self, data: bytes) -> None:
        self.written.append(data)

    async def drain(self) -> None:
        return None

    def close(self) -> None:
        self.closed = True

    async def wait_closed(self) -> None:
        return None


def _dial(monkeypatch: pytest.MonkeyPatch, payload: bytes) -> _Writer:
    """Patch the dial so `_irc_main` talks to an in-memory peer."""
    reader = asyncio.StreamReader()
    reader.feed_data(payload)
    reader.feed_eof()
    writer = _Writer()
    dialled: dict[str, Any] = {}

    async def fake_open(host: str, port: int, ssl: Any = None) -> tuple[Any, Any]:
        dialled.update(host=host, port=port, ssl=ssl)
        return reader, writer

    monkeypatch.setattr(asyncio, "open_connection", fake_open)
    writer.dialled = dialled  # type: ignore[attr-defined]
    return writer


def _gateway_for(monkeypatch: pytest.MonkeyPatch, **extra: str) -> dict[str, Any]:
    return _run(monkeypatch, **extra)


async def test_irc_main_registers_bootstraps_and_closes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen = _gateway_for(
        monkeypatch,
        PROFILE_ENCRYPTION_KEY=Fernet.generate_key().decode(),
        ARCHIVE_PSEUDONYM_KEY="long-random-operator-key",
        IRC_TLS="on",
        IRC_PASSWORD="s3cret",  # noqa: S106 -- test fixture, not a real credential
    )
    booted: list[str] = []

    async def boot_profiles(_self: object) -> None:
        booted.append("profiles")

    async def boot_messages(_self: object) -> None:
        booted.append("messages")

    monkeypatch.setattr(ToolsDbStore, "bootstrap", boot_profiles)
    monkeypatch.setattr(ToolsDbArchive, "bootstrap", boot_messages)
    writer = _dial(monkeypatch, b"PING :srv\r\n")

    await _REAL_IRC_MAIN(seen["config"], seen["gateway"], seen["store"], seen["archive"])

    assert booted == ["profiles", "messages"]
    sent = b"".join(writer.written).decode()
    assert sent.startswith("PASS s3cret\r\nNICK ")  # handshake, in RFC order
    assert "JOIN #wikipedia-fr\r\n" in sent
    assert sent.endswith("PONG :srv\r\n")  # the session answered the ping
    assert writer.closed  # the socket is released on the way out
    assert writer.dialled == {  # type: ignore[attr-defined]
        "host": "irc.libera.chat",
        "port": 6697,
        "ssl": True,
    }


async def test_irc_main_without_storage_skips_bootstrap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen = _gateway_for(monkeypatch, IRC_TLS="off", IRC_PORT="6667")
    writer = _dial(monkeypatch, b"")

    await _REAL_IRC_MAIN(seen["config"], seen["gateway"], None, None)

    assert writer.closed
    # Plaintext dials pass ssl=None, not ssl=False: asyncio treats False as "default".
    assert writer.dialled == {  # type: ignore[attr-defined]
        "host": "irc.libera.chat",
        "port": 6667,
        "ssl": None,
    }


async def test_irc_main_bootstraps_the_profile_store_alone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Without a pseudonym key there is no archive to bootstrap, only profiles."""
    seen = _gateway_for(monkeypatch, PROFILE_ENCRYPTION_KEY=Fernet.generate_key().decode())
    booted: list[str] = []

    async def boot_profiles(_self: object) -> None:
        booted.append("profiles")

    monkeypatch.setattr(ToolsDbStore, "bootstrap", boot_profiles)
    writer = _dial(monkeypatch, b"")

    await _REAL_IRC_MAIN(seen["config"], seen["gateway"], seen["store"], None)

    assert booted == ["profiles"]
    assert writer.closed


async def test_irc_main_contains_a_bootstrap_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen = _gateway_for(monkeypatch, PROFILE_ENCRYPTION_KEY=Fernet.generate_key().decode())

    async def boom(_self: object) -> None:
        raise StorageError

    monkeypatch.setattr(ToolsDbStore, "bootstrap", boom)
    writer = _dial(monkeypatch, b"")

    # A dead ToolsDB must not stop the bot from serving the channel.
    await _REAL_IRC_MAIN(seen["config"], seen["gateway"], seen["store"], None)
    assert writer.closed
