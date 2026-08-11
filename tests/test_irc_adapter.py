"""IRC adapter: protocol, scope, masker, transport and gateway (issue #19).

The line protocol is hand-rolled (no third-party IRC client), so it is
tested directly from strings — no socket, no mocked library internals.
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from typing import Any, cast

import pytest

import blybot.services.commands as sub
from blybot.adapters.irc.author_mask import IrcAuthorMasker
from blybot.adapters.irc.capabilities import IRC_CAPABILITIES
from blybot.adapters.irc.connection import IrcConnection
from blybot.adapters.irc.gateway import (
    REPLY_CAPTURE_USAGE,
    REPLY_HELP,
    IrcGateway,
    IrcSession,
)
from blybot.adapters.irc.ops import ChannelOps, strip_prefix
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

    async def send_lines(self, lines: Sequence[str]) -> None:
        if self._error is not None:
            raise self._error
        self.sent.extend(lines)

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
            directory=directory,
            groups=groups,
            page_url_for=str,
            counters=Counters(),
            capture_service=capture,
            capabilities=IRC_CAPABILITIES,
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


# --- operator tracking (#21) --------------------------------------------------


def test_names_prefixes_split_into_nick_and_authority() -> None:
    """Servers stack prefixes and disagree about which they use."""
    assert strip_prefix("@alice") == ("alice", True)
    assert strip_prefix("~owner") == ("owner", True)
    assert strip_prefix("&admin") == ("admin", True)
    assert strip_prefix("@+both") == ("both", True)  # stacked
    assert strip_prefix("+voiced") == ("voiced", False)  # voice is not authority
    assert strip_prefix("%halfop") == ("halfop", False)
    assert strip_prefix("plain") == ("plain", False)


def test_names_reply_is_the_authoritative_snapshot() -> None:
    ops = ChannelOps()
    ops.grant("#chan", "stale")
    ops.replace("#chan", ["@alice", "+bob", "carol"])
    assert ops.is_op("#chan", "alice")
    assert not ops.is_op("#chan", "bob")
    assert not ops.is_op("#chan", "stale")  # the snapshot replaces, not merges


def test_operator_status_is_case_insensitive_like_the_protocol() -> None:
    """A re-cased nick must not escape a de-op, nor a re-cased channel."""
    ops = ChannelOps()
    ops.grant("#Chan", "Alice")
    assert ops.is_op("#chan", "alice")
    assert ops.is_op("#CHAN", "ALICE")
    ops.revoke("#chan", "ALICE")
    assert not ops.is_op("#Chan", "Alice")


def test_leaving_the_network_or_renaming_drops_every_grant() -> None:
    """A freed nick can be claimed by someone else, so it keeps nothing."""
    ops = ChannelOps()
    ops.grant("#a", "alice")
    ops.grant("#b", "alice")
    ops.forget("Alice")
    assert not ops.is_op("#a", "alice")
    assert not ops.is_op("#b", "alice")


def test_forgetting_a_channel_drops_only_that_channel() -> None:
    ops = ChannelOps()
    ops.grant("#a", "alice")
    ops.grant("#b", "alice")
    ops.forget_channel("#A")
    assert not ops.is_op("#a", "alice")
    assert ops.is_op("#b", "alice")


def test_mode_changes_grant_and_revoke_in_order() -> None:
    ops = ChannelOps()
    ops.apply_mode("#chan", ["+oo", "alice", "bob"])
    assert ops.is_op("#chan", "alice")
    assert ops.is_op("#chan", "bob")
    ops.apply_mode("#chan", ["+o-o", "carol", "alice"])  # mixed in one line
    assert ops.is_op("#chan", "carol")
    assert not ops.is_op("#chan", "alice")


def test_mode_pairs_arguments_past_flags_that_are_not_authority() -> None:
    """`+vo bob alice` must op alice, not bob: `v` consumes its own argument."""
    ops = ChannelOps()
    ops.apply_mode("#chan", ["+vo", "bob", "alice"])
    assert ops.is_op("#chan", "alice")
    assert not ops.is_op("#chan", "bob")


def test_mode_ignores_no_argument_channel_flags() -> None:
    ops = ChannelOps()
    ops.apply_mode("#chan", ["+mo", "alice"])  # +m takes nothing
    assert ops.is_op("#chan", "alice")


def test_a_mode_line_we_cannot_pair_soundly_is_discarded_whole() -> None:
    """Mis-pairing would op the WRONG nick — the one failure that matters."""
    ops = ChannelOps()
    ops.apply_mode("#chan", ["+o"])  # truncated: no argument at all
    assert not ops.is_op("#chan", "alice")
    ops.apply_mode("#chan", ["+o", "alice", "unexpected-extra"])  # too many arguments
    assert not ops.is_op("#chan", "alice")
    ops.apply_mode("#chan", [])  # nothing at all
    assert not ops.is_op("#chan", "alice")


def test_setting_a_limit_consumes_an_argument_but_clearing_it_does_not() -> None:
    ops = ChannelOps()
    ops.apply_mode("#chan", ["+lo", "50", "alice"])
    assert ops.is_op("#chan", "alice")
    ops.apply_mode("#chan", ["-l+o", "bob"])  # -l carries no argument
    assert ops.is_op("#chan", "bob")


# --- command routing (#21) ----------------------------------------------------


def _op_gateway(store: InMemoryProfiles | None = None) -> IrcGateway:
    gateway, _archive = _gateway(store)
    gateway.ops.grant("#chan", "chanop")
    return gateway


async def test_an_unknown_verb_is_answered_with_silence() -> None:
    """A bot that answers every stray `!foo` is noise in a busy channel."""
    gateway = _op_gateway()
    assert await gateway.run_command("#chan", "chanop", "nonsense") is None
    assert await gateway.run_command("#chan", "chanop", "") is None


async def test_a_non_operator_is_refused_by_the_neutral_service() -> None:
    gateway = _op_gateway()
    reply = await gateway.run_command("#chan", "randomer", "capture on")
    assert reply == sub.REPLY_NOT_ADMIN
    # ...and the refusal is the SAME text Telegram and Discord give.


async def test_an_operator_can_toggle_capture() -> None:
    scope = scope_of("#chan")
    store = InMemoryProfiles(profiles={scope: GroupProfile(scope=scope)})
    gateway = _op_gateway(store)
    reply = await gateway.run_command("#chan", "chanop", "capture on")
    assert reply == sub.REPLY_CAPTURE_ENABLED
    assert store.profiles[scope].capture_enabled

    assert await gateway.run_command("#chan", "chanop", "capture") == REPLY_CAPTURE_USAGE
    assert await gateway.run_command("#chan", "chanop", "capture maybe") == REPLY_CAPTURE_USAGE


async def test_help_lists_the_surface_without_needing_op() -> None:
    gateway = _op_gateway()
    assert await gateway.run_command("#chan", "anyone", "help") == REPLY_HELP


async def test_every_dispatched_verb_reaches_the_neutral_service() -> None:
    """Each entry is one service call; none of them may raise on a bare run."""
    gateway = _op_gateway()
    for command in (
        "settings",
        "reset",
        "setpage Project:Somewhere",
        "setrepo owner/repo",
        "revoke",
        "llm show",
        "events on",
        "rule list",
        "rules",
        "action list",
        "issue something is broken",
        "repo",
    ):
        reply = await gateway.run_command("#chan", "chanop", command)
        assert reply, f"{command!r} produced no reply"


async def test_the_token_command_refuses_and_never_echoes_what_was_typed() -> None:
    """By the time this runs the secret is already public: warn, don't repeat."""
    gateway = _op_gateway()
    reply = await gateway.run_command("#chan", "chanop", "settoken ghp_supersecretvalue")
    assert reply == sub.REPLY_PAT_NO_PRIVATE_CHANNEL
    assert "ghp_supersecretvalue" not in (reply or "")
    assert "revoke" in (reply or "").lower()  # tells them what to do about it


async def test_the_token_refusal_does_not_depend_on_the_deployment() -> None:
    """A non-admin is still refused as a non-admin; anyone else gets the
    capability refusal even where repo actions are not wired at all."""
    gateway = _op_gateway()
    assert await gateway.run_command("#chan", "randomer", "settoken x") == sub.REPLY_NOT_ADMIN


# --- session-level membership + reply routing (#21) ---------------------------


def _session(gateway: IrcGateway) -> tuple[IrcSession, _RecordingChannel]:
    channel = _RecordingChannel()
    return (
        IrcSession(channel=cast("Any", channel), gateway=gateway, nick="blybot", channels=()),
        channel,
    )


async def _feed(session: IrcSession, *raws: str) -> None:
    for raw in raws:
        line = parse_line(raw)
        assert line is not None, raw
        await session.handle(line)


async def test_the_session_learns_operators_from_the_names_reply() -> None:
    """A restart re-learns from NAMES, so no identity is ever persisted."""
    gateway, _archive = _gateway()
    session, _channel = _session(gateway)
    await _feed(session, ":srv 353 blybot = #chan :@alice bob +carol")
    assert gateway.ops.is_op("#chan", "alice")
    assert not gateway.ops.is_op("#chan", "bob")


async def test_the_session_follows_mode_kick_part_quit_and_nick() -> None:
    gateway, _archive = _gateway()
    session, _channel = _session(gateway)
    await _feed(
        session,
        ":srv 353 blybot = #chan :@alice @bob @carol @dave",
        ":srv MODE #chan -o alice",
        ":op!u@h KICK #chan bob :bye",
        ":carol!u@h PART #chan :leaving",
        ":dave!u@h QUIT :connection reset",
    )
    for gone in ("alice", "bob", "carol", "dave"):
        assert not gateway.ops.is_op("#chan", gone), gone


async def test_a_rename_drops_the_old_nicks_authority() -> None:
    """The old name is freed and someone else may claim it."""
    gateway, _archive = _gateway()
    session, _channel = _session(gateway)
    await _feed(session, ":srv 353 blybot = #chan :@alice", ":alice!u@h NICK :alice_away")
    assert not gateway.ops.is_op("#chan", "alice")
    assert not gateway.ops.is_op("#chan", "alice_away")  # not carried over either


async def test_the_bot_leaving_forgets_the_whole_channel() -> None:
    gateway, _archive = _gateway()
    session, _channel = _session(gateway)
    await _feed(session, ":srv 353 blybot = #chan :@alice", ":blybot!u@h PART #chan :bye")
    assert not gateway.ops.is_op("#chan", "alice")


async def test_a_malformed_membership_line_changes_nothing() -> None:
    """A peer can send anything; a short line must not throw or mis-apply."""
    gateway, _archive = _gateway()
    session, _channel = _session(gateway)
    await _feed(
        session,
        ":srv 353 blybot = #chan :@alice",
        ":srv 353 blybot :truncated",  # too few params to name a channel
        ":srv MODE",  # no params at all
        ":op!u@h KICK #chan",  # no nick to kick
        "QUIT :no prefix, so nobody to forget",
        ":alice!u@h PART",  # no channel named
    )
    assert gateway.ops.is_op("#chan", "alice")  # untouched by any of them


async def test_a_command_reply_goes_to_the_channel_not_the_caller() -> None:
    """On IRC this is the consent model: the people being archived must see
    the announcement, and there is no ephemeral reply to hide it in."""
    scope = scope_of("#chan")
    store = InMemoryProfiles(profiles={scope: GroupProfile(scope=scope)})
    gateway, archive = _gateway(store)
    session, channel = _session(gateway)
    await _feed(session, ":srv 353 blybot = #chan :@chanop")
    await _feed(session, ":chanop!u@h PRIVMSG #chan :blybot: capture on")

    assert channel.sent, "the toggle produced no channel announcement"
    announcement = " ".join(line.split(" :", 1)[1] for line in channel.sent)
    assert announcement == sub.REPLY_CAPTURE_ENABLED
    assert all(line.startswith("PRIVMSG #chan :") for line in channel.sent)
    assert archive.messages == []  # the command itself is not archived


async def test_an_unrecognised_command_is_archived_as_ordinary_chatter() -> None:
    """Addressing the bot with nonsense is still just someone talking.

    Only a RECOGNISED command is withheld from the archive; dropping every
    line that happens to start with the bot's name would leave holes in a
    log the channel was told was being kept.
    """
    scope = scope_of("#chan")
    store = InMemoryProfiles(profiles={scope: GroupProfile(scope=scope, capture_enabled=True)})
    gateway, archive = _gateway(store)
    session, channel = _session(gateway)
    await _feed(session, ":n!u@h PRIVMSG #chan :blybot: what do you think?")
    assert channel.sent == []  # silence, not an error
    assert [m.text for m in archive.messages] == ["blybot: what do you think?"]


async def test_a_long_reply_is_split_across_protocol_lines() -> None:
    gateway, _archive = _gateway()
    session, channel = _session(gateway)
    await _feed(session, ":n!u@h PRIVMSG #chan :!help")
    assert channel.sent  # help needs no op
    for line in channel.sent:
        assert len(f"{line}\r\n".encode()) <= LINE_BYTES


# --- flood pacing + concurrent read/write on one socket (#67) -----------------


def _paced(payload: bytes = b"") -> tuple[IrcConnection, Any, list[float]]:
    """A connection whose clock and sleeps are recorded rather than real."""
    reader, writer = _pipe(payload)
    slept: list[float] = []
    now = [0.0]

    async def sleep(delay: float) -> None:
        slept.append(delay)
        now[0] += delay

    conn = IrcConnection(
        reader=reader,
        writer=cast("Any", writer),
        burst=2,
        min_interval=2.0,
        sleep=sleep,
        monotonic=lambda: now[0],
    )
    return conn, writer, slept


async def test_a_burst_is_allowed_then_lines_are_paced() -> None:
    """IRC servers kill a client that floods, with no warning and no
    retry-after — so a multi-line digest must not go out back-to-back."""
    conn, writer, slept = _paced()
    for index in range(4):
        await conn.send_line(f"PRIVMSG #chan :line {index}")

    assert len(writer.written) == 4  # every line still goes out...
    assert slept == [2.0, 2.0]  # ...the burst goes free, then one per interval


async def test_the_allowance_refills_while_the_bot_is_quiet() -> None:
    conn, _writer, slept = _paced()
    await conn.send_line("one")
    await conn.send_line("two")  # burst spent
    conn.monotonic = lambda: 10.0  # ten seconds of silence
    await conn.send_line("three")
    assert slept == []  # refilled: no wait needed


async def test_pacing_never_lets_two_writers_interleave_a_line() -> None:
    """The transport and the session share one socket; a half-written line
    would be a protocol error, not just garbled output."""
    conn, writer, _slept = _paced()
    await asyncio.gather(
        *(conn.send_line(f"PRIVMSG #chan :{name}") for name in ("alpha", "beta", "gamma"))
    )
    lines = b"".join(writer.written).decode().split("\r\n")[:-1]
    assert sorted(lines) == [
        "PRIVMSG #chan :alpha",
        "PRIVMSG #chan :beta",
        "PRIVMSG #chan :gamma",
    ]


async def test_a_delivery_loop_can_write_while_the_session_reads() -> None:
    """#67's core claim: background collectors share the session's socket."""
    scope = scope_of("#chan")
    store = InMemoryProfiles(profiles={scope: GroupProfile(scope=scope, capture_enabled=True)})
    gateway, archive = _gateway(store)
    reader, writer = _pipe(b":n!u@h PRIVMSG #chan :inbound\r\n")
    conn = IrcConnection(reader=reader, writer=cast("Any", writer))
    session = IrcSession(channel=conn, gateway=gateway, nick="blybot", channels=("#chan",))
    transport = IrcTransport(channel=conn)

    async def deliver() -> None:
        await transport.send(OutboundMessage(scope=scope, text="outbound digest"))

    await asyncio.gather(session.run(), deliver())

    assert [m.text for m in archive.messages] == ["inbound"]  # the read side worked
    assert b"PRIVMSG #chan :outbound digest\r\n" in b"".join(writer.written)


# --- bridge relay (#79) -------------------------------------------------------


class _RecordingRouter:
    def __init__(self) -> None:
        self.dispatched: list[Any] = []

    async def dispatch(self, message: Any) -> None:
        self.dispatched.append(message)


async def test_a_line_is_relayed_verbatim_and_archived_pseudonymously() -> None:
    """The two paths read the same line and never see each other's data."""
    scope = scope_of("#chan")
    store = InMemoryProfiles(profiles={scope: GroupProfile(scope=scope, capture_enabled=True)})
    gateway, archive = _gateway(store)
    router = _RecordingRouter()
    gateway.bridge = cast("Any", router)
    session, _channel = _session(gateway)

    await _feed(session, ":alice!u@h PRIVMSG #chan :hello everyone")

    (relayed,) = router.dispatched
    assert (relayed.author, relayed.text) == ("alice", "hello everyone")  # nick verbatim
    (stored,) = archive.messages
    assert stored.text == "hello everyone"
    assert "alice" not in stored.author  # ...but the archive only ever sees the label


async def test_relaying_needs_no_capture_and_capture_needs_no_bridge() -> None:
    """Neither feature may require the other to be configured."""
    gateway, archive = _gateway()  # capture has no profile enabling it
    router = _RecordingRouter()
    gateway.bridge = cast("Any", router)
    session, _channel = _session(gateway)

    await _feed(session, ":alice!u@h PRIVMSG #chan :still relayed")

    assert len(router.dispatched) == 1
    assert archive.messages == []


async def test_a_recognised_command_is_never_relayed() -> None:
    """A command is addressed to the bot, not said to the channel."""
    gateway, _archive = _gateway()
    router = _RecordingRouter()
    gateway.bridge = cast("Any", router)
    session, _channel = _session(gateway)

    await _feed(session, ":alice!u@h PRIVMSG #chan :!help")
    assert router.dispatched == []

    # ...but merely addressing the bot with nonsense is still talking.
    await _feed(session, ":alice!u@h PRIVMSG #chan :blybot: what do you think?")
    assert len(router.dispatched) == 1


async def test_no_bridge_configured_is_simply_a_noop() -> None:
    gateway, _archive = _gateway()
    assert gateway.bridge is None
    await gateway.relay_message("#chan", "alice", "hi")  # must not raise


async def test_two_concurrent_multi_line_messages_never_interleave() -> None:
    """The lock is per *message*, not per line (#80).

    Taking it per line let two senders interleave the pieces of two
    different multi-line messages — and a reader cannot tell that is what
    happened, which is what makes it worth a test rather than a comment.
    """
    conn, writer, _slept = _paced()
    transport = IrcTransport(channel=conn)
    long_a = "a" * 1200
    long_b = "b" * 1200

    await asyncio.gather(
        transport.send(OutboundMessage(scope=scope_of("#chan"), text=long_a)),
        transport.send(OutboundMessage(scope=scope_of("#chan"), text=long_b)),
    )

    payloads = [line.decode().split(" :", 1)[1].rstrip("\r\n") for line in writer.written]
    letters = ["".join(sorted(set(payload))) for payload in payloads]
    # Every line is purely one message's text, and one message's lines are
    # contiguous — no "abab" run anywhere.
    assert set(letters) == {"a", "b"}
    assert letters == sorted(letters) or letters == sorted(letters, reverse=True)


async def test_the_bridge_command_reaches_the_neutral_service() -> None:
    """One dispatch entry; every rule about consent lives in the service."""
    gateway = _op_gateway()
    refused = await gateway.run_command("#chan", "randomer", "bridge new")
    assert refused == sub.REPLY_NOT_ADMIN

    off = await gateway.run_command("#chan", "chanop", "bridge new")
    assert off == sub.REPLY_BRIDGE_OFF_DEPLOY  # no announcer on this deployment
