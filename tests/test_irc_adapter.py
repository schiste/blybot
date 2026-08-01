"""IRC adapter: protocol, scope, masker, transport and gateway (issue #19).

The line protocol is hand-rolled (no third-party IRC client), so it is
tested directly from strings — no socket, no mocked library internals.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any, cast

import pytest

from blybot.adapters.irc.author_mask import IrcAuthorMasker
from blybot.adapters.irc.capabilities import IRC_CAPABILITIES
from blybot.adapters.irc.connection import IrcConnection
from blybot.adapters.irc.gateway import IrcGateway, IrcSession
from blybot.adapters.irc.protocol import LINE_BYTES, parse_line, privmsg_lines
from blybot.adapters.irc.scope import irc_target, nick_scope, scope_of
from blybot.adapters.irc.transport import IrcTransport
from blybot.domain.models import ConsentMode, GroupProfile, OutboundMessage, Scope
from blybot.domain.ports import TransientTransportError
from blybot.observability import Counters
from blybot.services.capture import CaptureService
from blybot.services.commands import CommandService
from blybot.services.directory import ChannelDirectory
from blybot.services.policy import GroupPolicy, SlidingWindowLimiter
from tests.fakes import FakeClock, InMemoryArchive, InMemoryProfiles

NOW = datetime(2026, 8, 1, 12, 0, tzinfo=UTC)


# --- protocol ----------------------------------------------------------------


def test_parse_extracts_prefix_command_params_and_trailing() -> None:
    line = parse_line(":nick!user@host PRIVMSG #chan :hello there\r\n")
    assert line is not None
    assert (line.command, line.params, line.trailing) == ("PRIVMSG", ("#chan",), "hello there")
    assert line.nick == "nick"
    assert line.target == "#chan"


def test_parse_tolerates_anything_a_peer_can_send() -> None:
    """One malformed line must never end the read loop."""
    assert parse_line("") is None
    assert parse_line("   \r\n") is None
    assert parse_line(":prefix-only") is None  # a prefix with no command
    assert parse_line(":") is None
    assert parse_line(":pfx  :trailing") is None  # prefix, then a trailing but no command


def test_a_server_prefix_is_not_a_nick() -> None:
    """A server name has no '!'; attributing a message to it would be wrong."""
    line = parse_line(":irc.example.org NOTICE * :hi")
    assert line is not None
    assert line.nick == ""
    assert line.target == "*"


def test_a_line_without_params_reports_no_target() -> None:
    line = parse_line("PING :server")
    assert line is not None
    assert (line.command, line.target, line.trailing) == ("PING", "", "server")


def test_privmsg_lines_fit_the_protocol_byte_budget() -> None:
    lines = privmsg_lines("#chan", "x" * 2000)
    assert len(lines) > 1
    for line in lines:
        assert len(f"{line}\r\n".encode()) <= LINE_BYTES


def test_the_budget_shrinks_as_the_target_name_grows() -> None:
    """The 512 bytes cover the whole line, prefix included."""
    short = privmsg_lines("#a", "x" * 900)
    long = privmsg_lines("#" + "a" * 60, "x" * 900)
    assert len(long) >= len(short)


def test_multibyte_text_is_never_split_mid_codepoint() -> None:
    lines = privmsg_lines("#chan", "é" * 400)
    assert "".join(line.split(" :", 1)[1] for line in lines) == "é" * 400
    for line in lines:
        assert len(f"{line}\r\n".encode()) <= LINE_BYTES


def test_newlines_become_separate_lines_and_empty_text_sends_nothing() -> None:
    """A newline in a neutral message would otherwise be a protocol injection."""
    assert privmsg_lines("#chan", "one\ntwo") == ["PRIVMSG #chan :one", "PRIVMSG #chan :two"]
    assert privmsg_lines("#chan", "") == []


# --- scope + masker ----------------------------------------------------------


def test_channel_names_are_case_folded() -> None:
    """IRC compares channels case-insensitively; two cases must be one scope."""
    assert scope_of("#Foo") == scope_of("#foo") == Scope("irc", "#foo")
    assert nick_scope("Someone") == Scope("irc", "someone")
    assert irc_target(scope_of("#Foo")) == "#foo"


def test_the_masker_is_stable_per_channel_and_unlinkable_across_them() -> None:
    masker = IrcAuthorMasker(key="operator-key")
    assert masker.mask("#a", "nick") == masker.mask("#A", "NICK")  # case-folded
    assert masker.mask("#a", "nick") != masker.mask("#b", "nick")  # per-channel
    assert "nick" not in masker.mask("#a", "nick")  # never the raw identity
    assert IrcAuthorMasker(key="other").mask("#a", "nick") != masker.mask("#a", "nick")


# --- capabilities ------------------------------------------------------------


def test_capabilities_state_what_irc_cannot_do() -> None:
    """durable_dm=False is what gates digests off; bot_can_open_dm is separate
    and True, since a bot MAY privmsg a nick unprompted (#45)."""
    assert IRC_CAPABILITIES.durable_dm is False
    assert IRC_CAPABILITIES.bot_can_open_dm is True
    assert IRC_CAPABILITIES.threads is False
    assert IRC_CAPABILITIES.message_delete is False
    assert IRC_CAPABILITIES.rich_choices is False
    assert IRC_CAPABILITIES.max_message_chars > 0


# --- transport ---------------------------------------------------------------


class _RecordingChannel:
    def __init__(self, error: Exception | None = None) -> None:
        self.sent: list[str] = []
        self._error = error

    async def send_line(self, line: str) -> None:
        if self._error is not None:
            raise self._error
        self.sent.append(line)

    def lines(self) -> Any:  # pragma: no cover -- outbound-only
        raise NotImplementedError


async def test_transport_sends_privmsg_lines_for_the_scope() -> None:
    channel = _RecordingChannel()
    transport = IrcTransport(channel=cast("Any", channel))
    await transport.send(OutboundMessage(scope=scope_of("#chan"), text="hello"))
    assert channel.sent == ["PRIVMSG #chan :hello"]
    assert transport.capabilities is IRC_CAPABILITIES


async def test_transport_splits_a_long_message_across_lines() -> None:
    channel = _RecordingChannel()
    await IrcTransport(channel=cast("Any", channel)).send(
        OutboundMessage(scope=scope_of("#chan"), text="y" * 1500)
    )
    assert len(channel.sent) > 1


# --- connection --------------------------------------------------------------


def _pipe(payload: bytes) -> tuple[asyncio.StreamReader, Any]:
    reader = asyncio.StreamReader()
    reader.feed_data(payload)
    reader.feed_eof()

    class _Writer:
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

    return reader, _Writer()


async def test_connection_writes_crlf_and_reads_parsed_lines() -> None:
    reader, writer = _pipe(b":n!u@h PRIVMSG #c :hi\r\nPING :srv\r\n\r\n")
    conn = IrcConnection(reader=reader, writer=cast("Any", writer))
    await conn.send_line("NICK blybot")
    assert writer.written == [b"NICK blybot\r\n"]

    commands = [line.command async for line in conn.lines()]
    assert commands == ["PRIVMSG", "PING"]  # the blank line is dropped, not raised on
    await conn.close()
    assert writer.closed


async def test_a_broken_pipe_is_a_transient_error_the_loop_can_retry() -> None:
    reader, writer = _pipe(b"")

    def boom(_data: bytes) -> None:
        raise ConnectionResetError

    writer.write = boom
    conn = IrcConnection(reader=reader, writer=cast("Any", writer))
    with pytest.raises(TransientTransportError):
        await conn.send_line("NICK blybot")


async def test_a_dropped_connection_mid_read_is_transient_too() -> None:
    """A reset while reading must surface as the same retryable error as a write."""
    reader, writer = _pipe(b"")

    async def boom() -> bytes:
        raise ConnectionResetError

    reader.readline = boom  # type: ignore[method-assign]
    conn = IrcConnection(reader=reader, writer=cast("Any", writer))
    with pytest.raises(TransientTransportError):
        [line async for line in conn.lines()]


# --- gateway -----------------------------------------------------------------


def _gateway(store: InMemoryProfiles | None = None) -> tuple[IrcGateway, InMemoryArchive]:
    store = store if store is not None else InMemoryProfiles()
    archive = InMemoryArchive()
    clock = FakeClock(current=NOW)
    directory = ChannelDirectory(
        store=store,
        default_log_page="Project:Log",
        default_consent=ConsentMode.IMMEDIATE,
        default_repo="",
        page_suffix="IRC logs",
    )
    groups = GroupPolicy(allowed=set())
    capture = CaptureService(
        store=store,
        archive=archive,
        limiter=SlidingWindowLimiter(clock=clock, limit=100, window=timedelta(minutes=1)),
        clock=clock,
        counters=Counters(),
        max_chars=IRC_CAPABILITIES.max_message_chars,
    )
    gateway = IrcGateway(
        directory=directory,
        groups=groups,
        commands=CommandService(
            directory=directory, groups=groups, page_url_for=str, counters=Counters()
        ),
        clock=clock,
        nick="blybot",
        capture=capture,
        masker=IrcAuthorMasker(key="operator-key"),
    )
    return gateway, archive


def test_only_addressed_lines_read_as_commands() -> None:
    """IRC has no command registry, so ordinary chatter must never parse."""
    gateway, _archive = _gateway()
    assert gateway.addressed_command("blybot: help") == "help"
    assert gateway.addressed_command("Blybot, help") == "help"  # case and comma
    assert gateway.addressed_command("!help") == "help"
    assert gateway.addressed_command("I asked blybot about help") is None
    assert gateway.addressed_command("just talking") is None


async def test_capture_archives_only_an_enabled_allowed_channel() -> None:
    scope = scope_of("#chan")
    store = InMemoryProfiles(profiles={scope: GroupProfile(scope=scope, capture_enabled=True)})
    gateway, archive = _gateway(store)
    await gateway.ingest_message("#chan", "someone", "worth keeping")
    (stored,) = archive.messages
    assert stored.scope == scope
    assert stored.text == "worth keeping"
    assert "someone" not in stored.author  # pseudonymized at the boundary

    gateway.groups = GroupPolicy(allowed={"999"})
    await gateway.ingest_message("#chan", "someone", "not served")
    assert len(archive.messages) == 1


async def test_capture_is_a_noop_without_the_wiring() -> None:
    gateway, archive = _gateway()
    gateway.capture = None
    await gateway.ingest_message("#chan", "someone", "hi")
    assert archive.messages == []


# --- session -----------------------------------------------------------------


async def test_session_registers_joins_and_answers_ping() -> None:
    channel = _RecordingChannel()
    gateway, _archive = _gateway()
    session = IrcSession(
        channel=cast("Any", channel),
        gateway=gateway,
        nick="blybot",
        channels=("#a", "#b"),
        password="s3cret",  # noqa: S106 -- test fixture, not a real credential
    )
    await session.register()
    assert channel.sent[0] == "PASS s3cret"  # PASS precedes NICK/USER (RFC 1459)
    assert channel.sent[1:3] == ["NICK blybot", "USER blybot 0 * :blybot"]
    assert channel.sent[3:] == ["JOIN #a", "JOIN #b"]

    channel.sent.clear()
    ping = parse_line("PING :srv")
    assert ping is not None
    await session.handle(ping)
    assert channel.sent == ["PONG :srv"]  # missing this gets the session killed


async def test_session_ignores_its_own_echo_unattributed_lines_and_dms() -> None:
    scope = scope_of("#chan")
    store = InMemoryProfiles(profiles={scope: GroupProfile(scope=scope, capture_enabled=True)})
    gateway, archive = _gateway(store)
    session = IrcSession(
        channel=cast("Any", _RecordingChannel()), gateway=gateway, nick="blybot", channels=()
    )
    for raw in (
        ":blybot!u@h PRIVMSG #chan :my own message",
        "PRIVMSG #chan :no prefix, no author",
        ":n!u@h PRIVMSG blybot :a direct message",  # no durable_dm: nothing to do
        ":irc.example.org NOTICE #chan :server notice",
    ):
        line = parse_line(raw)
        assert line is not None
        await session.handle(line)
    assert archive.messages == []

    line = parse_line(":someone!u@h PRIVMSG #chan :a real line")
    assert line is not None
    await session.handle(line)
    assert [m.text for m in archive.messages] == ["a real line"]


async def test_session_run_registers_then_dispatches_until_eof() -> None:
    scope = scope_of("#chan")
    store = InMemoryProfiles(profiles={scope: GroupProfile(scope=scope, capture_enabled=True)})
    gateway, archive = _gateway(store)
    reader, writer = _pipe(b":n!u@h PRIVMSG #chan :one\r\n:n!u@h PRIVMSG #chan :two\r\n")
    conn = IrcConnection(reader=reader, writer=cast("Any", writer))
    await IrcSession(channel=conn, gateway=gateway, nick="blybot", channels=("#chan",)).run()
    assert [m.text for m in archive.messages] == ["one", "two"]
    assert b"JOIN #chan\r\n" in writer.written
