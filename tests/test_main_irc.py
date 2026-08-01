"""Composition-root tests for the IRC platform selector (`PLATFORM=irc`)."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any, cast

import pytest
from cryptography.fernet import Fernet

import blybot.__main__ as entry
from blybot.adapters.irc.capabilities import IRC_CAPABILITIES
from blybot.adapters.irc.transport import IrcTransport
from blybot.adapters.toolsdb.archive import ToolsDbArchive
from blybot.adapters.toolsdb.store import ToolsDbStore
from blybot.domain.ports import StorageError
from blybot.observability import Counters
from blybot.services.delivery import message_loop
from tests.test_config import REQUIRED

# Captured before `_run` patches it out, so the shell can still be exercised.
_REAL_IRC_MAIN = entry._irc_main
_REAL_MESSAGE_LOOP = message_loop

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

    def fake_main(
        config: Config, gateway: IrcGateway, store: Any, archive: Any, **kwargs: Any
    ) -> Any:
        seen.update(config=config, gateway=gateway, store=store, archive=archive, **kwargs)
        return _noop()

    monkeypatch.setattr(entry, "_irc_main", fake_main)
    # `run_irc` owns the event loop; the caller may already be inside one.
    monkeypatch.setattr(asyncio, "run", lambda coro: coro.close())
    assert entry.main() == 0
    return seen


async def _noop() -> None:
    return None  # pragma: no cover -- closed unstarted by the patched asyncio.run


def _labels(seen: dict[str, Any]) -> set[str]:
    return {label for _collector, label in seen["collectors"]}


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


# --- background delivery loops (#67) ------------------------------------------


async def test_a_full_deployment_runs_every_collector_irc_can_support(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen = _run(
        monkeypatch,
        PROFILE_ENCRYPTION_KEY=Fernet.generate_key().decode(),
        ARCHIVE_PSEUDONYM_KEY="long-random-operator-key",
        CAPTURE_REANNOUNCE_DAYS="30",
    )
    # No `sub_tick`: durable_dm=False gates digests off. No `session_sweep`:
    # IRC runs no DM transcription sessions to expire.
    assert _labels(seen) == {"action_tick", "repo_notify", "capture_remind"}
    await seen["release"]()  # closes the wiki, GitHub and LiftWing clients
    collectors = {label: collector for collector, label in seen["collectors"]}
    assert collectors["capture_remind"].cadence.days == 30
    engine = collectors["action_tick"].engine
    assert collectors["repo_notify"].engine is engine  # one engine per deployment
    assert set(engine.sources) == {"archive_window", "repo_events"}
    assert set(engine.sinks) == {"wiki_section", "reply", "chat_message", "chat_confirm"}


def test_the_reminder_needs_the_cadence_to_be_configured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen = _run(
        monkeypatch,
        PROFILE_ENCRYPTION_KEY=Fernet.generate_key().decode(),
        ARCHIVE_PSEUDONYM_KEY="long-random-operator-key",
    )
    assert _labels(seen) == {"action_tick", "repo_notify"}


async def test_a_store_only_deployment_notifies_but_schedules_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen = _run(monkeypatch, PROFILE_ENCRYPTION_KEY=Fernet.generate_key().decode())
    assert _labels(seen) == {"repo_notify"}  # scheduled analyses need the archive
    collectors = {label: collector for collector, label in seen["collectors"]}
    assert set(collectors["repo_notify"].engine.sources) == {"repo_events"}
    await seen["release"]()  # no LiftWing client on this tier to close


def test_a_minimal_deployment_has_no_background_work_at_all(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert _run(monkeypatch)["collectors"] == ()


async def test_each_collector_gets_a_delivery_loop_on_the_session_socket(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The loops write to the SAME socket the session reads from."""
    seen = _gateway_for(monkeypatch, EVENTS_POLL_MINUTES="7")
    _dial(monkeypatch, b"")
    spawned: list[tuple[Any, Any, float, str]] = []

    async def record(transport: Any, collector: Any, interval: float, label: str) -> None:
        spawned.append((transport, collector, interval, label))

    monkeypatch.setattr(entry, "message_loop", record)
    collector = cast("Any", object())
    await _REAL_IRC_MAIN(
        seen["config"],
        seen["gateway"],
        None,
        None,
        collectors=((collector, "action_tick"),),
    )

    (transport, given, interval, label) = spawned[0]
    assert (given, interval, label) == (collector, 420.0, "action_tick")
    assert isinstance(transport, IrcTransport)
    assert transport.capabilities is IRC_CAPABILITIES


async def test_the_loops_are_cancelled_when_the_peer_closes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A delivery loop left spinning against a dead socket would never exit."""
    seen = _gateway_for(monkeypatch)
    _dial(monkeypatch, b"")  # EOF immediately: the session ends at once
    started: list[str] = []

    async def forever(_transport: Any, _collector: Any, _interval: float, label: str) -> None:
        started.append(label)
        await asyncio.Event().wait()  # never completes on its own

    monkeypatch.setattr(entry, "message_loop", forever)
    released: list[int] = []

    async def release() -> None:
        released.append(1)

    await _REAL_IRC_MAIN(
        seen["config"],
        seen["gateway"],
        None,
        None,
        collectors=((cast("Any", object()), "repo_notify"),),
        counters=Counters(),
        release=release,
    )
    assert started == ["repo_notify"]  # it ran...
    assert released == [1]  # ...and _irc_main still returned, clients closed
