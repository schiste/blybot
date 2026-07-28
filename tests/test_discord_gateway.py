"""Discord gateway: pure delegate logic and the thin discord.py shell.

The :class:`DiscordGateway` methods are tested directly with plain
arguments (no discord objects); the :class:`DiscordGatewayClient` shell is
tested by invoking its ``on_message`` method and each registered slash
command's callback with lightweight fakes standing in for the SDK objects
(mirroring how the Telegram/PTB handlers are tested against a fake bot).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, cast

import discord
import pytest

from blybot.adapters.discord import gateway as gw
from blybot.adapters.discord.author_mask import DiscordAuthorMasker
from blybot.adapters.discord.gateway import (
    DiscordGateway,
    DiscordGatewayClient,
    build_gateway_client,
    default_intents,
)
from blybot.domain.models import ConsentMode, GroupProfile, Schedule, Scope
from blybot.domain.subscriptions import Subscription
from blybot.observability import Counters
from blybot.services import commands as cmd
from blybot.services.capture import CaptureService
from blybot.services.commands import CommandService
from blybot.services.directory import ChannelDirectory
from blybot.services.policy import GroupPolicy, SlidingWindowLimiter
from tests.fakes import FakeClock, InMemoryArchive, InMemoryProfiles, InMemorySubscriptions

if TYPE_CHECKING:
    from blybot.domain.ports import ProfileStore

_CHANNEL = 555000111
_SCOPE = Scope("discord", str(_CHANNEL))


def _directory(
    store: ProfileStore | None, *, page_suffix: str = "Discord logs"
) -> ChannelDirectory:
    return ChannelDirectory(
        store=store,
        default_log_page="Project:Log",
        default_consent=ConsentMode.IMMEDIATE,
        default_repo="",
        page_suffix=page_suffix,
    )


def _make_gateway(
    directory: ChannelDirectory,
    groups: GroupPolicy,
    *,
    capture: CaptureService | None = None,
    masker: DiscordAuthorMasker | None = None,
    subscriptions: InMemorySubscriptions | None = None,
    default_lang: str = "en",
) -> DiscordGateway:
    """A gateway wired to a matching CommandService (identity page_url_for)."""
    commands = CommandService(
        directory=directory,
        groups=groups,
        page_url_for=str,
        counters=Counters(),
        capture_service=capture,
    )
    return DiscordGateway(
        directory=directory,
        groups=groups,
        commands=commands,
        capture=capture,
        masker=masker,
        subscriptions=subscriptions,
        default_lang=default_lang,
    )


def _capture_service(
    store: ProfileStore, archive: InMemoryArchive, *, max_chars: int = 2000
) -> CaptureService:
    clock = FakeClock()
    return CaptureService(
        store=store,
        archive=archive,
        limiter=SlidingWindowLimiter(clock=clock, limit=1000, window=timedelta(minutes=1)),
        clock=clock,
        counters=Counters(),
        max_chars=max_chars,
    )


def _capture_gateway(
    *, allowed: set[int] | None = None, store: InMemoryProfiles | None = None
) -> tuple[DiscordGateway, InMemoryProfiles, InMemoryArchive, CaptureService]:
    store = store if store is not None else InMemoryProfiles()
    archive = InMemoryArchive()
    capture = _capture_service(store, archive)
    gateway = _make_gateway(
        _directory(store),
        GroupPolicy(allowed=allowed if allowed is not None else set()),
        capture=capture,
        masker=DiscordAuthorMasker(key="operator-key"),
    )
    return gateway, store, archive, capture


# --- DiscordGateway.ingest_message -------------------------------------------


async def test_ingest_is_a_noop_without_capture_wiring() -> None:
    archive = InMemoryArchive()
    gateway = _make_gateway(_directory(None), GroupPolicy(allowed=set()))
    await gateway.ingest_message(
        channel_id=_CHANNEL,
        thread_id=None,
        author_id=7,
        message_id=1,
        posted_at=datetime(2026, 7, 20, tzinfo=UTC),
        text="hi",
        reply_to=None,
    )
    assert archive.messages == []  # nothing wired: silently ignored


async def test_ingest_skips_a_channel_outside_the_allowlist() -> None:
    gateway, _store, archive, _capture = _capture_gateway(allowed={999})
    await gateway.ingest_message(
        channel_id=_CHANNEL,
        thread_id=None,
        author_id=7,
        message_id=1,
        posted_at=datetime(2026, 7, 20, tzinfo=UTC),
        text="hi",
        reply_to=None,
    )
    assert archive.messages == []


async def test_ingest_archives_a_capture_enabled_channel_pseudonymously() -> None:
    store = InMemoryProfiles(profiles={_SCOPE: GroupProfile(scope=_SCOPE, capture_enabled=True)})
    gateway, _store, archive, _capture = _capture_gateway(store=store)
    await gateway.ingest_message(
        channel_id=_CHANNEL,
        thread_id=None,
        author_id=7,
        message_id=42,
        posted_at=datetime(2026, 7, 20, tzinfo=UTC),
        text="hello",
        reply_to=None,
    )
    (stored,) = archive.messages
    assert stored.scope == _SCOPE
    assert stored.kind == "text"
    assert stored.text == "hello"
    assert stored.author  # pseudonymized label present
    assert stored.author != "7"  # never the raw author id


async def test_ingest_records_media_and_reply_metadata_in_a_thread() -> None:
    thread_scope = Scope("discord", str(_CHANNEL), "999")
    store = InMemoryProfiles(
        profiles={thread_scope: GroupProfile(scope=thread_scope, capture_enabled=True)}
    )
    gateway, _store, archive, _capture = _capture_gateway(store=store)
    await gateway.ingest_message(
        channel_id=_CHANNEL,
        thread_id=999,
        author_id=7,
        message_id=42,
        posted_at=datetime(2026, 7, 20, tzinfo=UTC),
        text="",
        reply_to=41,
    )
    (stored,) = archive.messages
    assert stored.scope == thread_scope
    assert stored.kind == "media_note"
    assert stored.reply_to == 41


# --- DiscordGateway.capture_command ------------------------------------------


async def test_capture_command_rejects_non_admins() -> None:
    gateway, _store, _archive, _capture = _capture_gateway()
    reply = await gateway.capture_command(_CHANNEL, None, enabled=True, is_admin=False)
    assert reply == cmd.REPLY_NOT_ADMIN


async def test_capture_command_reports_when_capture_is_off_on_the_deployment() -> None:
    gateway = _make_gateway(_directory(None), GroupPolicy(allowed=set()))
    reply = await gateway.capture_command(_CHANNEL, None, enabled=True, is_admin=True)
    assert reply == cmd.REPLY_CAPTURE_OFF_DEPLOY


async def test_capture_command_refuses_a_channel_outside_the_allowlist() -> None:
    gateway, _store, _archive, _capture = _capture_gateway(allowed={999})
    reply = await gateway.capture_command(_CHANNEL, None, enabled=True, is_admin=True)
    assert reply == cmd.REPLY_NOT_ALLOWED


async def test_capture_command_enables_and_disables() -> None:
    gateway, store, _archive, _capture = _capture_gateway()
    on = await gateway.capture_command(_CHANNEL, None, enabled=True, is_admin=True)
    assert on == cmd.REPLY_CAPTURE_ENABLED
    assert store.profiles[_SCOPE].capture_enabled is True
    off = await gateway.capture_command(_CHANNEL, None, enabled=False, is_admin=True)
    assert off == cmd.REPLY_CAPTURE_DISABLED
    assert store.profiles[_SCOPE].capture_enabled is False


async def test_capture_command_tombstones_on_a_failed_disable() -> None:
    store = InMemoryProfiles(fail=True)
    gateway, _store, _archive, capture = _capture_gateway(store=store)
    reply = await gateway.capture_command(_CHANNEL, None, enabled=False, is_admin=True)
    assert reply == cmd.REPLY_STORAGE_DOWN
    assert _SCOPE in capture._denied  # fail-closed until the disable lands


async def test_capture_command_does_not_tombstone_a_failed_enable() -> None:
    store = InMemoryProfiles(fail=True)
    gateway, _store, _archive, capture = _capture_gateway(store=store)
    reply = await gateway.capture_command(_CHANNEL, None, enabled=True, is_admin=True)
    assert reply == cmd.REPLY_STORAGE_DOWN
    assert _SCOPE not in capture._denied  # a failed enable already fails safe (stays off)


# --- DiscordGateway.setpage_command ------------------------------------------


async def test_setpage_command_rejects_non_admins() -> None:
    gateway = _make_gateway(_directory(InMemoryProfiles()), GroupPolicy(allowed=set()))
    assert await gateway.setpage_command(_CHANNEL, None, "P", is_admin=False) == cmd.REPLY_NOT_ADMIN


async def test_setpage_command_shows_usage_for_a_blank_page() -> None:
    gateway = _make_gateway(_directory(InMemoryProfiles()), GroupPolicy(allowed=set()))
    assert await gateway.setpage_command(
        _CHANNEL, None, "   ", is_admin=True
    ) == cmd.REPLY_SETPAGE_USAGE.format(suffix="Discord logs")


async def test_setpage_command_stores_the_composed_page() -> None:
    store = InMemoryProfiles()
    gateway = _make_gateway(_directory(store), GroupPolicy(allowed=set()))
    reply = await gateway.setpage_command(_CHANNEL, None, "WikiProject Foo", is_admin=True)
    assert "WikiProject Foo/Discord logs" in reply
    assert store.profiles[_SCOPE].log_page == "WikiProject Foo/Discord logs"


async def test_setpage_command_refuses_an_invalid_page() -> None:
    gateway = _make_gateway(_directory(InMemoryProfiles()), GroupPolicy(allowed=set()))
    reply = await gateway.setpage_command(_CHANNEL, None, "bad|title", is_admin=True)
    assert reply == cmd.REPLY_PAGE_REFUSED.format(suffix="Discord logs")


async def test_setpage_command_reports_when_self_service_is_off() -> None:
    gateway = _make_gateway(
        _directory(InMemoryProfiles(), page_suffix=""), GroupPolicy(allowed=set())
    )
    reply = await gateway.setpage_command(_CHANNEL, None, "WikiProject Foo", is_admin=True)
    assert reply == cmd.REPLY_SELF_SERVICE_OFF


async def test_setpage_command_reports_storage_down() -> None:
    gateway = _make_gateway(_directory(InMemoryProfiles(fail=True)), GroupPolicy(allowed=set()))
    reply = await gateway.setpage_command(_CHANNEL, None, "WikiProject Foo", is_admin=True)
    assert reply == cmd.REPLY_STORAGE_DOWN


# --- DiscordGateway subscription commands ------------------------------------


def _subs_gateway(
    *, store: InMemoryProfiles | None = None, subs: InMemorySubscriptions | None = None
) -> tuple[DiscordGateway, InMemoryProfiles, InMemorySubscriptions]:
    store = store if store is not None else InMemoryProfiles()
    subs = subs if subs is not None else InMemorySubscriptions()
    gateway = _make_gateway(
        _directory(store),
        GroupPolicy(allowed=set()),
        subscriptions=subs,
        default_lang="en",
    )
    return gateway, store, subs


async def test_subscribe_reports_when_unavailable() -> None:
    gateway = _make_gateway(_directory(InMemoryProfiles()), GroupPolicy(allowed=set()))
    assert await gateway.subscribe_command(_CHANNEL, None, 321, "") == gw.REPLY_SUBS_UNAVAILABLE


async def test_subscribe_surfaces_a_parse_error() -> None:
    gateway, _store, _subs = _subs_gateway()
    reply = await gateway.subscribe_command(_CHANNEL, None, 321, "notaschedule")
    assert "Didn't understand" in reply


async def test_subscribe_mints_a_code_and_stores_the_subscription() -> None:
    gateway, store, subs = _subs_gateway()
    reply = await gateway.subscribe_command(_CHANNEL, None, 321, "daily@08:00 summarize")
    assert "Subscribed" in reply
    assert store.profiles[_SCOPE].subscribe_code is not None  # channel became subscribable
    (stored,) = subs.subs.values()
    assert stored.dm == Scope("discord", "321")
    assert stored.scope == _SCOPE


async def test_subscribe_keeps_an_existing_code() -> None:
    store = InMemoryProfiles(
        profiles={_SCOPE: GroupProfile(scope=_SCOPE, subscribe_code="already")}
    )
    gateway, _store, _subs = _subs_gateway(store=store)
    await gateway.subscribe_command(_CHANNEL, None, 321, "")
    assert store.profiles[_SCOPE].subscribe_code == "already"  # not re-minted


async def test_subscribe_reports_storage_down() -> None:
    gateway, _store, _subs = _subs_gateway(subs=InMemorySubscriptions(fail=True))
    reply = await gateway.subscribe_command(_CHANNEL, None, 321, "")
    assert reply == gw.REPLY_STORAGE_DOWN


async def test_mysubs_reports_when_unavailable() -> None:
    gateway = _make_gateway(_directory(InMemoryProfiles()), GroupPolicy(allowed=set()))
    assert await gateway.mysubs_command(321) == gw.REPLY_SUBS_UNAVAILABLE


async def test_mysubs_reports_storage_down() -> None:
    gateway, _store, _subs = _subs_gateway(subs=InMemorySubscriptions(fail=True))
    assert await gateway.mysubs_command(321) == gw.REPLY_STORAGE_DOWN


async def test_mysubs_reports_no_subscriptions() -> None:
    gateway, _store, _subs = _subs_gateway()
    assert await gateway.mysubs_command(321) == gw.REPLY_NO_SUBS


async def test_mysubs_lists_the_callers_subscriptions() -> None:
    subs = InMemorySubscriptions()
    dm = Scope("discord", "321")
    subs.subs["abcd"] = Subscription(
        sub_id="abcd",
        dm=dm,
        scope=_SCOPE,
        schedule=Schedule(kind="daily", hour=8),
        recipe="summarize",
        lang="en",
    )
    gateway, _store, _subs = _subs_gateway(subs=subs)
    reply = await gateway.mysubs_command(321)
    assert gw.REPLY_SUBS_HEADER in reply
    assert "[abcd] daily@08:00 summarize (en)" in reply


async def test_unsubscribe_reports_when_unavailable() -> None:
    gateway = _make_gateway(_directory(InMemoryProfiles()), GroupPolicy(allowed=set()))
    assert await gateway.unsubscribe_command(321, "abcd") == gw.REPLY_SUBS_UNAVAILABLE


async def test_unsubscribe_shows_usage_for_a_blank_id() -> None:
    gateway, _store, _subs = _subs_gateway()
    assert await gateway.unsubscribe_command(321, "  ") == gw.REPLY_UNSUB_USAGE


async def test_unsubscribe_removes_a_matching_subscription() -> None:
    subs = InMemorySubscriptions()
    dm = Scope("discord", "321")
    subs.subs["abcd"] = Subscription(
        sub_id="abcd",
        dm=dm,
        scope=_SCOPE,
        schedule=Schedule(kind="daily", hour=8),
        recipe="summarize",
        lang="en",
    )
    gateway, _store, _subs = _subs_gateway(subs=subs)
    assert await gateway.unsubscribe_command(321, "abcd") == gw.REPLY_UNSUBSCRIBED
    assert await gateway.unsubscribe_command(321, "abcd") == gw.REPLY_NO_SUCH_SUB  # already gone


async def test_unsubscribe_reports_storage_down() -> None:
    gateway, _store, _subs = _subs_gateway(subs=InMemorySubscriptions(fail=True))
    assert await gateway.unsubscribe_command(321, "abcd") == gw.REPLY_STORAGE_DOWN


# --- helpers on the shell ----------------------------------------------------


def test_default_intents_request_only_message_content() -> None:
    """Least privilege: message content is the one privileged intent we need."""
    intents = default_intents()
    assert intents.message_content is True
    assert intents.members is False  # join detection not wired — do not request it


def test_channel_ids_splits_threads_from_plain_channels() -> None:
    assert gw._channel_ids(SimpleNamespace(id=100)) == (100, None)
    assert gw._channel_ids(SimpleNamespace(id=999, parent_id=100)) == (100, 999)


def test_is_admin_reads_live_guild_permissions() -> None:
    assert (
        gw._is_admin(SimpleNamespace(guild_permissions=SimpleNamespace(administrator=True))) is True
    )
    assert (
        gw._is_admin(SimpleNamespace(guild_permissions=SimpleNamespace(administrator=False)))
        is False
    )
    assert gw._is_admin(SimpleNamespace()) is False  # a DM user has no guild permissions


# --- DiscordGatewayClient shell ----------------------------------------------


class _Response:
    """A discord InteractionResponse stand-in recording ephemeral replies."""

    def __init__(self) -> None:
        self.sent: list[tuple[str, bool]] = []

    async def send_message(self, content: str, *, ephemeral: bool = False) -> None:
        self.sent.append((content, ephemeral))


def _admin_interaction(channel: object) -> Any:
    user = SimpleNamespace(guild_permissions=SimpleNamespace(administrator=True))
    return SimpleNamespace(channel=channel, user=user, response=_Response())


def _dm_interaction(channel: object, dm_id: int = 321) -> Any:
    async def create_dm() -> SimpleNamespace:
        return SimpleNamespace(id=dm_id)

    return SimpleNamespace(
        channel=channel, user=SimpleNamespace(create_dm=create_dm), response=_Response()
    )


def _command(client: DiscordGatewayClient, name: str) -> Any:
    return cast("Any", client.tree.get_command(name))


def test_build_registers_the_five_slash_commands() -> None:
    gateway, _store, _archive, _capture = _capture_gateway()
    client = build_gateway_client(gateway)
    names = sorted(command.name for command in client.tree.get_commands())
    assert names == ["capture", "mysubs", "setpage", "subscribe", "unsubscribe"]
    assert client.intents.message_content is True  # default privileged intents


def test_build_accepts_explicit_intents() -> None:
    gateway, _store, _archive, _capture = _capture_gateway()
    intents = discord.Intents.none()
    client = build_gateway_client(gateway, intents=intents)
    assert client.intents.message_content is False  # the passed intents were used verbatim


async def test_setup_hook_syncs_commands_and_runs_the_startup_hook(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gateway, _store, _archive, _capture = _capture_gateway()
    started: list[str] = []

    async def on_setup(_client: DiscordGatewayClient) -> None:
        started.append("setup")

    client = build_gateway_client(gateway, on_setup=on_setup)
    synced: list[int] = []

    async def fake_sync() -> list[object]:
        synced.append(1)
        return []

    monkeypatch.setattr(client.tree, "sync", fake_sync)  # avoid the network call
    await client.setup_hook()
    assert synced == [1]
    assert started == ["setup"]


async def test_setup_hook_without_a_startup_hook_only_syncs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gateway, _store, _archive, _capture = _capture_gateway()
    client = build_gateway_client(gateway)
    synced: list[int] = []

    async def fake_sync() -> list[object]:
        synced.append(1)
        return []

    monkeypatch.setattr(client.tree, "sync", fake_sync)
    await client.setup_hook()
    assert synced == [1]


async def test_on_message_ignores_bots() -> None:
    store = InMemoryProfiles(profiles={_SCOPE: GroupProfile(scope=_SCOPE, capture_enabled=True)})
    gateway, _store, archive, _capture = _capture_gateway(store=store)
    client = build_gateway_client(gateway)
    message = SimpleNamespace(author=SimpleNamespace(bot=True))
    await client.on_message(cast("discord.Message", message))
    assert archive.messages == []


async def test_on_message_archives_a_plain_channel_post() -> None:
    store = InMemoryProfiles(profiles={_SCOPE: GroupProfile(scope=_SCOPE, capture_enabled=True)})
    gateway, _store, archive, _capture = _capture_gateway(store=store)
    client = build_gateway_client(gateway)
    message = SimpleNamespace(
        author=SimpleNamespace(bot=False, id=7),
        channel=SimpleNamespace(id=_CHANNEL),
        id=42,
        created_at=datetime(2026, 7, 20, tzinfo=UTC),
        content="hi there",
        reference=None,
    )
    await client.on_message(cast("discord.Message", message))
    (stored,) = archive.messages
    assert stored.scope == _SCOPE
    assert stored.text == "hi there"
    assert stored.reply_to is None


async def test_on_message_archives_a_thread_reply() -> None:
    thread_scope = Scope("discord", str(_CHANNEL), "999")
    store = InMemoryProfiles(
        profiles={thread_scope: GroupProfile(scope=thread_scope, capture_enabled=True)}
    )
    gateway, _store, archive, _capture = _capture_gateway(store=store)
    client = build_gateway_client(gateway)
    message = SimpleNamespace(
        author=SimpleNamespace(bot=False, id=7),
        channel=SimpleNamespace(id=999, parent_id=_CHANNEL),
        id=43,
        created_at=datetime(2026, 7, 20, tzinfo=UTC),
        content="reply",
        reference=SimpleNamespace(message_id=41),
    )
    await client.on_message(cast("discord.Message", message))
    (stored,) = archive.messages
    assert stored.scope == thread_scope
    assert stored.reply_to == 41


async def test_capture_slash_command_answers_ephemerally() -> None:
    gateway, _store, _archive, _capture = _capture_gateway()
    client = build_gateway_client(gateway)
    interaction = _admin_interaction(SimpleNamespace(id=_CHANNEL))
    await _command(client, "capture").callback(interaction, "on")
    assert interaction.response.sent == [(cmd.REPLY_CAPTURE_ENABLED, True)]


async def test_setpage_slash_command_answers_ephemerally() -> None:
    gateway, store, _archive, _capture = _capture_gateway()
    client = build_gateway_client(gateway)
    interaction = _admin_interaction(SimpleNamespace(id=_CHANNEL))
    await _command(client, "setpage").callback(interaction, "WikiProject Foo")
    assert interaction.response.sent[0][1] is True
    assert store.profiles[_SCOPE].log_page == "WikiProject Foo/Discord logs"


async def test_subscribe_slash_command_uses_the_callers_dm_channel() -> None:
    store = InMemoryProfiles()
    subs = InMemorySubscriptions()
    gateway = _make_gateway(_directory(store), GroupPolicy(allowed=set()), subscriptions=subs)
    client = build_gateway_client(gateway)
    interaction = _dm_interaction(SimpleNamespace(id=_CHANNEL), dm_id=321)
    await _command(client, "subscribe").callback(interaction, "daily@08:00 summarize")
    (stored,) = subs.subs.values()
    assert stored.dm == Scope("discord", "321")
    assert interaction.response.sent[0][1] is True


async def test_mysubs_slash_command_reports_no_subscriptions() -> None:
    gateway = _make_gateway(
        _directory(InMemoryProfiles()),
        GroupPolicy(allowed=set()),
        subscriptions=InMemorySubscriptions(),
    )
    client = build_gateway_client(gateway)
    interaction = _dm_interaction(SimpleNamespace(id=_CHANNEL))
    await _command(client, "mysubs").callback(interaction)
    assert interaction.response.sent == [(gw.REPLY_NO_SUBS, True)]


async def test_unsubscribe_slash_command_reports_unknown_id() -> None:
    gateway = _make_gateway(
        _directory(InMemoryProfiles()),
        GroupPolicy(allowed=set()),
        subscriptions=InMemorySubscriptions(),
    )
    client = build_gateway_client(gateway)
    interaction = _dm_interaction(SimpleNamespace(id=_CHANNEL))
    await _command(client, "unsubscribe").callback(interaction, "nope")
    assert interaction.response.sent == [(gw.REPLY_NO_SUCH_SUB, True)]


def test_run_starts_the_client() -> None:
    started: list[str] = []

    class _FakeClient:
        def run(self, token: str) -> None:
            started.append(token)

    gw.run(cast("discord.Client", _FakeClient()), "bot-token")
    assert started == ["bot-token"]
