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
from blybot.adapters.discord.capabilities import DISCORD_CAPABILITIES
from blybot.adapters.discord.gateway import (
    DiscordGateway,
    DiscordGatewayClient,
    build_gateway_client,
    default_intents,
)
from blybot.adapters.discord.scope import dm_scope
from blybot.domain.models import (
    ConsentMode,
    GroupProfile,
    LlmSettings,
    OutboundMessage,
    Schedule,
    Scope,
    TimestampGranularity,
)
from blybot.domain.ports import WikiWriteError
from blybot.domain.subscriptions import Subscription
from blybot.observability import Counters
from blybot.services import analysis_run as ar
from blybot.services import commands as cmd
from blybot.services.analysis_run import AnalysisService
from blybot.services.capture import CaptureService
from blybot.services.commands import CommandService
from blybot.services.directory import ChannelDirectory
from blybot.services.dm_routing import DmRouteRegistry
from blybot.services.engine import ActionEngine
from blybot.services.policy import GroupPolicy, SlidingWindowLimiter
from blybot.services.publish import (
    ChatConfirmSink,
    LogPublicationService,
    LogPublishTransform,
)
from blybot.services.repo import GroupRepoService
from blybot.services.sessions import SessionRegistry
from blybot.services.transcribe import DmTranscriptionService
from tests.fakes import (
    FakeClock,
    FakePublisher,
    FakeRepoGateway,
    FakeSink,
    FakeSource,
    InMemoryActions,
    InMemoryArchive,
    InMemoryProfiles,
    InMemorySubscriptions,
    PassthroughSanitizer,
    SequentialPseudonyms,
    SuffixTransform,
)

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
    analysis: AnalysisService | None = None,
    default_lang: str = "en",
    vault: InMemoryProfiles | None = None,
    llm_defaults: LlmSettings | None = None,
) -> DiscordGateway:
    """A gateway wired to a matching CommandService (identity page_url_for)."""
    commands = CommandService(
        directory=directory,
        groups=groups,
        page_url_for=str,
        counters=Counters(),
        capture_service=capture,
        vault=vault,
        llm_defaults=llm_defaults,
        llm_max_tokens_ceiling=4096,
        subscriptions=subscriptions,
        capabilities=DISCORD_CAPABILITIES,
        default_lang=default_lang,
    )
    return DiscordGateway(
        directory=directory,
        groups=groups,
        commands=commands,
        capture=capture,
        masker=masker,
        subscriptions=subscriptions,
        analysis=analysis,
        default_lang=default_lang,
    )


_CONFIRMATION = OutboundMessage(scope=_SCOPE, text="Published: url")


def _analysis_service(source: FakeSource | None = None) -> AnalysisService:
    """An AnalysisService whose engine mirrors the on-demand recipe wiring."""
    clock = FakeClock()
    engine = ActionEngine(
        sources={"archive_window": source or FakeSource(payload="transcript")},
        transforms={"prompt": SuffixTransform(), "stats": SuffixTransform()},
        sinks={"wiki_section": FakeSink(messages=(_CONFIRMATION,))},
        counters=Counters(),
        clock=clock,
    )
    return AnalysisService(
        engine=engine,
        limiter=SlidingWindowLimiter(clock=clock, limit=10, window=timedelta(hours=1)),
        clock=clock,
        counters=Counters(),
    )


def _analysis_gateway(analysis: AnalysisService | None) -> DiscordGateway:
    return _make_gateway(
        _directory(InMemoryProfiles()), GroupPolicy(allowed=set()), analysis=analysis
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
    *, allowed: set[str] | None = None, store: InMemoryProfiles | None = None
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
    gateway, _store, archive, _capture = _capture_gateway(allowed={"999"})
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
    reply = (await gateway.capture_command(_CHANNEL, None, enabled=True, is_admin=False)).text
    assert reply == cmd.REPLY_NOT_ADMIN


async def test_capture_command_reports_when_capture_is_off_on_the_deployment() -> None:
    gateway = _make_gateway(_directory(None), GroupPolicy(allowed=set()))
    reply = (await gateway.capture_command(_CHANNEL, None, enabled=True, is_admin=True)).text
    assert reply == cmd.REPLY_CAPTURE_OFF_DEPLOY


async def test_capture_command_refuses_a_channel_outside_the_allowlist() -> None:
    gateway, _store, _archive, _capture = _capture_gateway(allowed={"999"})
    reply = (await gateway.capture_command(_CHANNEL, None, enabled=True, is_admin=True)).text
    assert reply == cmd.REPLY_NOT_ALLOWED


async def test_capture_command_enables_and_disables() -> None:
    gateway, store, _archive, _capture = _capture_gateway()
    on = await gateway.capture_command(_CHANNEL, None, enabled=True, is_admin=True)
    assert on.text == cmd.REPLY_CAPTURE_ENABLED
    assert on.ok is True  # drives the PUBLIC announcement in the shell
    assert store.profiles[_SCOPE].capture_enabled is True
    off = await gateway.capture_command(_CHANNEL, None, enabled=False, is_admin=True)
    assert off.text == cmd.REPLY_CAPTURE_DISABLED
    assert store.profiles[_SCOPE].capture_enabled is False


async def test_capture_command_tombstones_on_a_failed_disable() -> None:
    store = InMemoryProfiles(fail=True)
    gateway, _store, _archive, capture = _capture_gateway(store=store)
    reply = (await gateway.capture_command(_CHANNEL, None, enabled=False, is_admin=True)).text
    assert reply == cmd.REPLY_STORAGE_DOWN
    assert _SCOPE in capture._denied  # fail-closed until the disable lands


async def test_capture_command_does_not_tombstone_a_failed_enable() -> None:
    store = InMemoryProfiles(fail=True)
    gateway, _store, _archive, capture = _capture_gateway(store=store)
    reply = (await gateway.capture_command(_CHANNEL, None, enabled=True, is_admin=True)).text
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


# --- DiscordGateway config commands (settings/reset/revoke/llm) --------------


def _admin_gateway(
    *, store: InMemoryProfiles | None = None, with_llm: bool = True
) -> tuple[DiscordGateway, InMemoryProfiles]:
    """A gateway whose CommandService carries the vault + (optional) LLM defaults."""
    store = store if store is not None else InMemoryProfiles()
    gateway = _make_gateway(
        _directory(store),
        GroupPolicy(allowed=set()),
        vault=store,
        llm_defaults=LlmSettings() if with_llm else None,
    )
    return gateway, store


async def test_settings_command_rejects_non_admins() -> None:
    gateway, _store = _admin_gateway()
    assert await gateway.settings_command(_CHANNEL, None, is_admin=False) == cmd.REPLY_NOT_ADMIN


async def test_settings_command_reports_the_configuration() -> None:
    gateway, _store = _admin_gateway()
    reply = await gateway.settings_command(_CHANNEL, None, is_admin=True)
    assert "(all defaults)" in reply
    assert "message capture: off" in reply


async def test_reset_command_rejects_non_admins() -> None:
    gateway, _store = _admin_gateway()
    assert await gateway.reset_command(_CHANNEL, None, is_admin=False) == cmd.REPLY_NOT_ADMIN


async def test_reset_command_forgets_the_profile() -> None:
    gateway, store = _admin_gateway()
    await gateway.setpage_command(_CHANNEL, None, "WikiProject Foo", is_admin=True)
    reply = await gateway.reset_command(_CHANNEL, None, is_admin=True)
    assert reply == cmd.REPLY_RESET
    assert store.profiles == {}


async def test_revoke_command_rejects_non_admins() -> None:
    gateway, _store = _admin_gateway()
    assert await gateway.revoke_command(_CHANNEL, None, is_admin=False) == cmd.REPLY_NOT_ADMIN


async def test_revoke_command_discards_the_token() -> None:
    gateway, store = _admin_gateway()
    await store.store_token(_SCOPE, "ghp_x")
    reply = await gateway.revoke_command(_CHANNEL, None, is_admin=True)
    assert reply == cmd.REPLY_REVOKED
    assert store.tokens == {}


async def test_llm_command_rejects_non_admins() -> None:
    gateway, _store = _admin_gateway()
    assert await gateway.llm_command(_CHANNEL, None, "show", is_admin=False) == cmd.REPLY_NOT_ADMIN


async def test_llm_command_reports_when_off_on_the_deployment() -> None:
    gateway, _store = _admin_gateway(with_llm=False)
    assert (
        await gateway.llm_command(_CHANNEL, None, "show", is_admin=True) == cmd.REPLY_LLM_OFF_DEPLOY
    )


async def test_llm_command_sets_and_shows() -> None:
    gateway, store = _admin_gateway()
    confirmation = await gateway.llm_command(
        _CHANNEL, None, "set model:large lang:fr", is_admin=True
    )
    assert "model:large" in confirmation
    assert store.profiles[_SCOPE].llm == LlmSettings(model="large", lang="fr")
    shown = await gateway.llm_command(_CHANNEL, None, "show", is_admin=True)
    assert cmd._LLM_ORIGIN_OWN in shown


async def test_llm_command_shows_usage_for_no_arguments() -> None:
    gateway, _store = _admin_gateway()
    assert await gateway.llm_command(_CHANNEL, None, "", is_admin=True) == cmd.REPLY_LLM_USAGE


# --- DiscordGateway.analyze_command ------------------------------------------


async def test_analyze_reports_when_analyses_are_unavailable() -> None:
    gateway = _analysis_gateway(analysis=None)  # deployment without an archive
    reply = await gateway.analyze_command(
        _CHANNEL, None, command="summarize", recipe="summarize", is_admin=True
    )
    assert reply == gw.REPLY_ANALYSES_UNAVAILABLE


async def test_analyze_refuses_non_admins() -> None:
    gateway = _analysis_gateway(_analysis_service())
    reply = await gateway.analyze_command(
        _CHANNEL, None, command="summarize", recipe="summarize", is_admin=False
    )
    assert reply == ar.REPLY_NOT_ADMIN


async def test_analyze_publishes_and_returns_the_confirmation() -> None:
    gateway = _analysis_gateway(_analysis_service())
    reply = await gateway.analyze_command(
        _CHANNEL, None, command="stats", recipe="stats", is_admin=True
    )
    assert reply == "Published: url"


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
    assert await gateway.subscribe_command(_CHANNEL, None, 321, "") == cmd.REPLY_SUBS_UNAVAILABLE


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
    # An explicit option, so this exercises the storage path rather than the
    # inherit check a bare subscribe now runs first (#73).
    reply = await gateway.subscribe_command(_CHANNEL, None, 321, "stats")
    assert reply == cmd.REPLY_STORAGE_DOWN


async def test_mysubs_reports_when_unavailable() -> None:
    gateway = _make_gateway(_directory(InMemoryProfiles()), GroupPolicy(allowed=set()))
    assert await gateway.mysubs_command(321) == cmd.REPLY_SUBS_UNAVAILABLE


async def test_mysubs_reports_storage_down() -> None:
    gateway, _store, _subs = _subs_gateway(subs=InMemorySubscriptions(fail=True))
    assert await gateway.mysubs_command(321) == cmd.REPLY_STORAGE_DOWN


async def test_mysubs_reports_no_subscriptions() -> None:
    gateway, _store, _subs = _subs_gateway()
    assert await gateway.mysubs_command(321) == cmd.REPLY_NO_SUBS


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
    assert cmd.REPLY_SUBS_HEADER in reply
    assert "[abcd] daily@08:00 summarize (en)" in reply


async def test_unsubscribe_reports_when_unavailable() -> None:
    gateway = _make_gateway(_directory(InMemoryProfiles()), GroupPolicy(allowed=set()))
    assert await gateway.unsubscribe_command(321, "abcd") == cmd.REPLY_SUBS_UNAVAILABLE


async def test_unsubscribe_shows_usage_for_a_blank_id() -> None:
    gateway, _store, _subs = _subs_gateway()
    assert await gateway.unsubscribe_command(321, "  ") == cmd.REPLY_UNSUB_USAGE


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
    assert await gateway.unsubscribe_command(321, "abcd") == cmd.REPLY_UNSUBSCRIBED
    assert await gateway.unsubscribe_command(321, "abcd") == cmd.REPLY_NO_SUCH_SUB  # already gone


async def test_unsubscribe_reports_storage_down() -> None:
    gateway, _store, _subs = _subs_gateway(subs=InMemorySubscriptions(fail=True))
    assert await gateway.unsubscribe_command(321, "abcd") == cmd.REPLY_STORAGE_DOWN


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
    """A discord InteractionResponse stand-in recording replies and modals."""

    def __init__(self) -> None:
        self.sent: list[tuple[str, bool]] = []
        self.modals: list[Any] = []

    async def send_message(self, content: str, *, ephemeral: bool = False) -> None:
        self.sent.append((content, ephemeral))

    async def send_modal(self, modal: Any) -> None:
        self.modals.append(modal)


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


def test_build_registers_every_slash_command() -> None:
    gateway, _store, _archive, _capture = _capture_gateway()
    client = build_gateway_client(gateway)
    # Context menus share the tree with slash commands; split them out.
    entries = client.tree.get_commands()
    names = sorted(c.name for c in entries if not isinstance(c, discord.app_commands.ContextMenu))
    assert names == [
        "action",
        "bridge",
        "capture",
        "events",
        "issue",
        "llm",
        "mysubs",
        "repo",
        "reset",
        "revoke",
        "rule",
        "rules",
        "setconsent",
        "setpage",
        "setrepo",
        "settings",
        "settoken",
        "stats",
        "subscribable",
        "subscribe",
        "summarize",
        "talkingpoints",
        "transcribe",
        "unsubscribe",
    ]
    # /log is a message context menu, not a slash command — Discord's
    # equivalent of Telegram's reply-to-a-message gesture.
    menus = [c for c in entries if isinstance(c, discord.app_commands.ContextMenu)]
    assert [m.name for m in menus] == [gw.LOG_MENU_LABEL]
    # /action and /rule are groups: Discord routes their subcommands natively.
    (actions,) = [c for c in client.tree.get_commands() if c.name == "action"]
    assert sorted(sub.name for sub in cast("Any", actions).commands) == ["add", "list", "remove"]
    (group,) = [c for c in client.tree.get_commands() if c.name == "rule"]
    assert sorted(sub.name for sub in cast("Any", group).commands) == ["add", "clear", "remove"]
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
        guild=SimpleNamespace(id=1),
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
        guild=SimpleNamespace(id=1),
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
    # PUBLIC, not ephemeral: on a platform with no privacy mode this reply is
    # the channel's only notice that its messages are now archived (#17).
    assert interaction.response.sent == [(cmd.REPLY_CAPTURE_ENABLED, False)]


async def test_setpage_slash_command_answers_ephemerally() -> None:
    gateway, store, _archive, _capture = _capture_gateway()
    client = build_gateway_client(gateway)
    interaction = _admin_interaction(SimpleNamespace(id=_CHANNEL))
    await _command(client, "setpage").callback(interaction, "WikiProject Foo")
    assert interaction.response.sent[0][1] is True
    assert store.profiles[_SCOPE].log_page == "WikiProject Foo/Discord logs"


async def test_settings_slash_command_answers_ephemerally() -> None:
    gateway, _store = _admin_gateway()
    client = build_gateway_client(gateway)
    interaction = _admin_interaction(SimpleNamespace(id=_CHANNEL))
    await _command(client, "settings").callback(interaction)
    (content, ephemeral) = interaction.response.sent[0]
    assert ephemeral is True
    assert "(all defaults)" in content


async def test_reset_slash_command_answers_ephemerally() -> None:
    gateway, store = _admin_gateway()
    client = build_gateway_client(gateway)
    await gateway.setpage_command(_CHANNEL, None, "WikiProject Foo", is_admin=True)
    interaction = _admin_interaction(SimpleNamespace(id=_CHANNEL))
    await _command(client, "reset").callback(interaction)
    assert interaction.response.sent == [(cmd.REPLY_RESET, True)]
    assert store.profiles == {}


async def test_revoke_slash_command_answers_ephemerally() -> None:
    gateway, store = _admin_gateway()
    await store.store_token(_SCOPE, "ghp_x")
    client = build_gateway_client(gateway)
    interaction = _admin_interaction(SimpleNamespace(id=_CHANNEL))
    await _command(client, "revoke").callback(interaction)
    assert interaction.response.sent == [(cmd.REPLY_REVOKED, True)]
    assert store.tokens == {}


async def test_llm_slash_command_sets_and_answers_ephemerally() -> None:
    gateway, store = _admin_gateway()
    client = build_gateway_client(gateway)
    interaction = _admin_interaction(SimpleNamespace(id=_CHANNEL))
    await _command(client, "llm").callback(interaction, "set model:large")
    assert interaction.response.sent[0][1] is True
    assert store.profiles[_SCOPE].llm == LlmSettings(model="large")


async def test_llm_slash_command_defaults_to_usage_without_arguments() -> None:
    gateway, _store = _admin_gateway()
    client = build_gateway_client(gateway)
    interaction = _admin_interaction(SimpleNamespace(id=_CHANNEL))
    await _command(client, "llm").callback(interaction)
    assert interaction.response.sent == [(cmd.REPLY_LLM_USAGE, True)]


class _DeferInteraction:
    """A slash interaction recording the defer→run→follow-up ordering.

    Its ``response.defer`` and ``followup.send`` append to a shared event
    log, and the analysis engine's source appends ``"run"`` between them, so
    a test can prove the interaction is acknowledged *before* the long
    analysis starts — Discord kills an un-deferred interaction after 3s.
    """

    def __init__(self, channel: object, events: list[Any], *, admin: bool = True) -> None:
        self.channel = channel
        self.user = SimpleNamespace(guild_permissions=SimpleNamespace(administrator=admin))
        self._events = events
        self.response = SimpleNamespace(defer=self._defer)
        self.followup = SimpleNamespace(send=self._send)

    async def _defer(self) -> None:
        self._events.append("defer")

    async def _send(self, content: str) -> None:
        self._events.append(("send", content))


class _RecordingSource:
    """An archive-window source that records the run order and the built spec."""

    def __init__(self, events: list[Any], specs: list[Any]) -> None:
        self._events = events
        self._specs = specs

    async def fetch(self, context: Any) -> str:
        self._events.append("run")
        self._specs.append(context.spec)
        return "transcript"


async def _drive_analysis(name: str) -> tuple[list[Any], list[Any]]:
    events: list[Any] = []
    specs: list[Any] = []
    analysis = _analysis_service(source=cast("Any", _RecordingSource(events, specs)))
    gateway = _analysis_gateway(analysis)
    client = build_gateway_client(gateway)
    interaction = _DeferInteraction(SimpleNamespace(id=_CHANNEL), events)
    await _command(client, name).callback(cast("Any", interaction))
    return events, specs


async def test_summarize_slash_command_defers_before_running_then_follows_up() -> None:
    events, specs = await _drive_analysis("summarize")
    # Acknowledged first, THEN the long run, THEN the confirmation follow-up.
    assert events == ["defer", "run", ("send", "Published: url")]
    assert specs[0].trigger.command == "summarize"
    assert specs[0].transforms[0].param("template") == "summarize"


async def test_stats_slash_command_runs_the_stats_recipe() -> None:
    events, specs = await _drive_analysis("stats")
    assert events == ["defer", "run", ("send", "Published: url")]
    assert specs[0].transforms[0].name == "stats"  # the deterministic recipe


async def test_talkingpoints_slash_command_maps_to_the_talking_points_recipe() -> None:
    events, specs = await _drive_analysis("talkingpoints")
    assert events == ["defer", "run", ("send", "Published: url")]
    assert specs[0].transforms[0].param("template") == "talking_points"


async def test_analysis_slash_command_refuses_a_non_admin_after_deferring() -> None:
    events: list[Any] = []
    gateway = _analysis_gateway(_analysis_service())
    client = build_gateway_client(gateway)
    interaction = _DeferInteraction(SimpleNamespace(id=_CHANNEL), events, admin=False)
    await _command(client, "summarize").callback(cast("Any", interaction))
    # Still deferred first (the deadline applies before the admit check),
    # then the refusal arrives as the follow-up.
    assert events == ["defer", ("send", ar.REPLY_NOT_ADMIN)]


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
    assert interaction.response.sent == [(cmd.REPLY_NO_SUBS, True)]


async def test_unsubscribe_slash_command_reports_unknown_id() -> None:
    gateway = _make_gateway(
        _directory(InMemoryProfiles()),
        GroupPolicy(allowed=set()),
        subscriptions=InMemorySubscriptions(),
    )
    client = build_gateway_client(gateway)
    interaction = _dm_interaction(SimpleNamespace(id=_CHANNEL))
    await _command(client, "unsubscribe").callback(interaction, "nope")
    assert interaction.response.sent == [(cmd.REPLY_NO_SUCH_SUB, True)]


def test_run_starts_the_client() -> None:
    started: list[str] = []

    class _FakeClient:
        def run(self, token: str) -> None:
            started.append(token)

    gw.run(cast("discord.Client", _FakeClient()), "bot-token")
    assert started == ["bot-token"]


# --- repo notifications: /events, /rule, /rules (issue #40) ------------------


async def test_events_command_maps_the_scope_and_delegates() -> None:
    gateway, store = _admin_gateway()
    assert await gateway.events_command(_CHANNEL, None, "on", is_admin=False) == cmd.REPLY_NOT_ADMIN
    # No repo bound at this channel yet, so the neutral guard answers.
    assert (
        await gateway.events_command(_CHANNEL, None, "on", is_admin=True)
        == cmd.REPLY_EVENTS_NEED_REPO
    )
    await gateway.directory.set_repo(_SCOPE, "org/repo")
    assert (
        await gateway.events_command(_CHANNEL, None, "on", is_admin=True) == cmd.REPLY_EVENTS_SEEDED
    )
    assert store.profiles[_SCOPE].events_enabled is True
    assert await gateway.events_command(_CHANNEL, None, "junk", is_admin=True) == (
        cmd.REPLY_EVENTS_USAGE
    )


async def test_rule_commands_add_list_remove_and_clear() -> None:
    gateway, store = _admin_gateway()
    added = await gateway.rule_add_command(_CHANNEL, None, "pr.merged base:main", is_admin=True)
    (rule,) = store.profiles[_SCOPE].rules
    assert rule.rule_id in added

    listing = await gateway.rules_command(_CHANNEL, None, is_admin=True)
    assert listing.startswith("Rules for this scope:")

    removed = await gateway.rule_remove_command(_CHANNEL, None, rule.rule_id, is_admin=True)
    assert removed == cmd.REPLY_RULE_REMOVED.format(id=rule.rule_id)

    await gateway.rule_add_command(_CHANNEL, None, "release", is_admin=True)
    cleared = await gateway.rule_clear_command(_CHANNEL, None, is_admin=True)
    assert cleared == cmd.REPLY_RULES_CLEARED.format(count=1)
    assert store.profiles[_SCOPE].rules == ()


async def test_rule_commands_are_admin_gated() -> None:
    gateway, _store = _admin_gateway()
    replies = [
        await gateway.rule_add_command(_CHANNEL, None, "release", is_admin=False),
        await gateway.rule_remove_command(_CHANNEL, None, "abcd", is_admin=False),
        await gateway.rule_clear_command(_CHANNEL, None, is_admin=False),
        await gateway.rules_command(_CHANNEL, None, is_admin=False),
    ]
    assert replies == [cmd.REPLY_NOT_ADMIN] * len(replies)


async def test_events_and_rule_slash_commands_answer_ephemerally() -> None:
    gateway, store = _admin_gateway()
    await gateway.directory.set_repo(_SCOPE, "org/repo")
    client = build_gateway_client(gateway)
    channel = SimpleNamespace(id=_CHANNEL)
    rule_group = cast("Any", _command(client, "rule"))
    leaves = {sub.name: sub for sub in rule_group.commands}

    interaction = _admin_interaction(channel)
    await _command(client, "events").callback(interaction, "on")
    assert interaction.response.sent == [(cmd.REPLY_EVENTS_SEEDED, True)]

    interaction = _admin_interaction(channel)
    await leaves["add"].callback(interaction, "issue.opened label:bug")
    assert interaction.response.sent[0][1] is True

    interaction = _admin_interaction(channel)
    await _command(client, "rules").callback(interaction)
    (listing, ephemeral) = interaction.response.sent[0]
    assert ephemeral is True
    assert "issue.opened label:bug" in listing

    added = store.profiles[_SCOPE].rules[-1]
    interaction = _admin_interaction(channel)
    await leaves["remove"].callback(interaction, added.rule_id)
    assert interaction.response.sent == [(cmd.REPLY_RULE_REMOVED.format(id=added.rule_id), True)]

    interaction = _admin_interaction(channel)
    await leaves["clear"].callback(interaction)
    assert interaction.response.sent == [(cmd.REPLY_RULES_CLEARED.format(count=2), True)]
    assert store.profiles[_SCOPE].rules == ()


# --- /setrepo + the /settoken modal (issue #43, increment 2) ------------------

# A fixture value, not a secret: FakeRepoGateway accepts only this one.
_GOOD_PAT = "ghp_good"


def _repo_gateway() -> tuple[DiscordGateway, InMemoryProfiles, FakeRepoGateway]:
    store = InMemoryProfiles()
    actions = FakeRepoGateway(valid_tokens={_GOOD_PAT})
    directory = _directory(store)
    groups = GroupPolicy(allowed=set())
    commands = CommandService(
        directory=directory,
        groups=groups,
        page_url_for=str,
        counters=Counters(),
        vault=store,
        repo_actions=actions,
    )
    gateway = DiscordGateway(directory=directory, groups=groups, commands=commands)
    return gateway, store, actions


async def test_setrepo_command_binds_and_points_at_the_modal() -> None:
    gateway, store, _actions = _repo_gateway()
    assert (
        await gateway.setrepo_command(_CHANNEL, None, "org/repo", is_admin=False)
        == cmd.REPLY_NOT_ADMIN
    )
    refused = await gateway.setrepo_command(_CHANNEL, None, "not-a-repo", is_admin=True)
    assert refused == cmd.REPLY_SETREPO_USAGE  # no next-step hint on a refusal

    bound = await gateway.setrepo_command(_CHANNEL, None, "org/repo", is_admin=True)
    assert bound.startswith(cmd.REPLY_REPO_BOUND.format(repo="org/repo"))
    assert gw.REPLY_PAT_NEXT_STEP in bound  # Discord's own way to hand over the secret
    assert store.profiles[_SCOPE].repo == "org/repo"


async def test_settoken_command_delegates_validation_and_storage() -> None:
    gateway, store, _actions = _repo_gateway()
    await gateway.setrepo_command(_CHANNEL, None, "org/repo", is_admin=True)
    assert (
        await gateway.settoken_command(_CHANNEL, None, _GOOD_PAT, is_admin=False)
        == cmd.REPLY_NOT_ADMIN
    )
    assert store.tokens == {}
    assert (
        await gateway.settoken_command(_CHANNEL, None, _GOOD_PAT, is_admin=True)
        == cmd.REPLY_PAT_SAVED
    )
    assert store.tokens[_SCOPE] == _GOOD_PAT


async def test_setrepo_slash_command_answers_ephemerally() -> None:
    gateway, store, _actions = _repo_gateway()
    client = build_gateway_client(gateway)
    interaction = _admin_interaction(SimpleNamespace(id=_CHANNEL))
    await _command(client, "setrepo").callback(interaction, "org/repo")
    (content, ephemeral) = interaction.response.sent[0]
    assert ephemeral is True
    assert gw.REPLY_PAT_NEXT_STEP in content
    assert store.profiles[_SCOPE].repo == "org/repo"


async def test_settoken_slash_command_opens_the_modal_for_admins_only() -> None:
    gateway, _store, _actions = _repo_gateway()
    client = build_gateway_client(gateway)
    channel = SimpleNamespace(id=_CHANNEL)

    outsider = SimpleNamespace(
        channel=channel,
        user=SimpleNamespace(guild_permissions=SimpleNamespace(administrator=False)),
        response=_Response(),
    )
    await _command(client, "settoken").callback(cast("Any", outsider))
    # Refused before any form is shown; no modal was ever sent.
    assert outsider.response.sent == [(cmd.REPLY_NOT_ADMIN, True)]
    assert outsider.response.modals == []

    interaction = _admin_interaction(channel)
    await _command(client, "settoken").callback(interaction)
    (modal,) = interaction.response.modals
    assert isinstance(modal, gw.TokenModal)
    assert modal.title == gw.PAT_MODAL_TITLE


async def test_token_modal_submit_stores_the_secret_without_posting_it() -> None:
    gateway, store, _actions = _repo_gateway()
    await gateway.setrepo_command(_CHANNEL, None, "org/repo", is_admin=True)
    modal = gw.TokenModal(gateway, _CHANNEL, None)
    modal.token_input._value = _GOOD_PAT  # what Discord puts in the payload

    interaction = _admin_interaction(SimpleNamespace(id=_CHANNEL))
    await modal.on_submit(interaction)
    assert interaction.response.sent == [(cmd.REPLY_PAT_SAVED, True)]
    assert store.tokens[_SCOPE] == _GOOD_PAT


async def test_token_modal_rechecks_admin_at_submit_time() -> None:
    """The form may have been opened before the caller lost their role."""
    gateway, store, _actions = _repo_gateway()
    await gateway.setrepo_command(_CHANNEL, None, "org/repo", is_admin=True)
    modal = gw.TokenModal(gateway, _CHANNEL, None)
    modal.token_input._value = _GOOD_PAT

    demoted = SimpleNamespace(
        channel=SimpleNamespace(id=_CHANNEL),
        user=SimpleNamespace(guild_permissions=SimpleNamespace(administrator=False)),
        response=_Response(),
    )
    await modal.on_submit(cast("Any", demoted))
    assert demoted.response.sent == [(cmd.REPLY_NOT_ADMIN, True)]
    assert store.tokens == {}


# --- /issue and /repo (issue #42) --------------------------------------------


def _issue_gateway() -> tuple[DiscordGateway, InMemoryProfiles, FakeRepoGateway]:
    store = InMemoryProfiles()
    actions = FakeRepoGateway(valid_tokens={_GOOD_PAT})
    directory = _directory(store)
    groups = GroupPolicy(allowed=set())
    commands = CommandService(
        directory=directory,
        groups=groups,
        page_url_for=str,
        counters=Counters(),
        repo_service=GroupRepoService(gateway=actions, vault=store, directory=directory),
        repo_limiter=SlidingWindowLimiter(clock=FakeClock(), limit=10, window=timedelta(minutes=1)),
    )
    gateway = DiscordGateway(directory=directory, groups=groups, commands=commands)
    return gateway, store, actions


async def test_issue_and_repo_commands_delegate_to_the_neutral_service() -> None:
    gateway, store, actions = _issue_gateway()
    unbound = await gateway.issue_command(_CHANNEL, None, "broken")
    assert unbound == cmd.REPLY_ISSUE_UNBOUND

    await gateway.directory.set_repo(_SCOPE, "org/repo")
    await store.store_token(_SCOPE, _GOOD_PAT)
    filed = await gateway.issue_command(_CHANNEL, None, "broken")
    assert "github.com/org/repo/issues" in filed
    assert actions.issues[0][0] == "org/repo"

    assert "org/repo" in await gateway.repo_command(_CHANNEL, None)


async def test_issue_and_repo_slash_commands_stay_ephemeral() -> None:
    """Ephemeral is load-bearing: a public reply would name the reporter."""
    gateway, store, _actions = _issue_gateway()
    await gateway.directory.set_repo(_SCOPE, "org/repo")
    await store.store_token(_SCOPE, _GOOD_PAT)
    client = build_gateway_client(gateway)
    channel = SimpleNamespace(id=_CHANNEL)

    interaction = _admin_interaction(channel)
    await _command(client, "issue").callback(interaction, "the button is broken")
    (content, ephemeral) = interaction.response.sent[0]
    assert ephemeral is True
    assert "github.com/org/repo/issues" in content

    interaction = _admin_interaction(channel)
    await _command(client, "repo").callback(interaction)
    (summary, ephemeral) = interaction.response.sent[0]
    assert ephemeral is True
    assert "org/repo" in summary


async def test_subscribe_refuses_past_the_per_user_cap() -> None:
    """Issue #23: one subscriber cannot create unbounded rows."""
    store = InMemoryProfiles()
    subs = InMemorySubscriptions()
    directory = _directory(store)
    groups = GroupPolicy(allowed=set())
    gateway = _make_gateway(directory, groups, subscriptions=subs)
    gateway.commands.max_subs_per_user = 2

    for _ in range(2):
        assert "Subscribed" in await gateway.subscribe_command(_CHANNEL, None, 321, "stats")
    refused = await gateway.subscribe_command(_CHANNEL, None, 321, "stats")
    assert "maximum of 2" in refused
    assert len(subs.subs) == 2  # nothing extra was written

    # A different subscriber is unaffected, and the existing rows survive.
    assert "Subscribed" in await gateway.subscribe_command(_CHANNEL, None, 999, "stats")
    assert len(await subs.list_for_user(dm_scope(321))) == 2


# --- /action: scheduled analyses (issue #43, increment 3) --------------------


def _action_gateway() -> tuple[DiscordGateway, InMemoryActions]:
    store = InMemoryProfiles()
    actions = InMemoryActions()
    directory = _directory(store)
    groups = GroupPolicy(allowed=set())
    commands = CommandService(
        directory=directory,
        groups=groups,
        page_url_for=str,
        counters=Counters(),
        actions=actions,
        clock=FakeClock(),
    )
    return DiscordGateway(directory=directory, groups=groups, commands=commands), actions


async def test_action_commands_are_admin_gated_and_round_trip() -> None:
    gateway, actions = _action_gateway()
    assert (
        await gateway.action_add_command(_CHANNEL, None, "daily@06:00 summarize", is_admin=False)
        == cmd.REPLY_NOT_ADMIN
    )
    assert actions.actions == {}

    added = await gateway.action_add_command(_CHANNEL, None, "daily@06:00 summarize", is_admin=True)
    (spec,) = actions.actions[_SCOPE]
    assert spec.action_id in added

    listing = await gateway.action_list_command(_CHANNEL, None, is_admin=True)
    assert listing.startswith("Scheduled actions for this scope:")

    removed = await gateway.action_remove_command(_CHANNEL, None, spec.action_id, is_admin=True)
    assert removed == cmd.REPLY_ACTION_REMOVED.format(id=spec.action_id)
    assert actions.actions[_SCOPE] == ()

    assert await gateway.action_list_command(_CHANNEL, None, is_admin=False) == cmd.REPLY_NOT_ADMIN
    assert (
        await gateway.action_remove_command(_CHANNEL, None, "x", is_admin=False)
        == cmd.REPLY_NOT_ADMIN
    )


async def test_action_slash_subcommands_answer_ephemerally() -> None:
    gateway, actions = _action_gateway()
    client = build_gateway_client(gateway)
    channel = SimpleNamespace(id=_CHANNEL)
    group = cast("Any", _command(client, "action"))
    leaves = {sub.name: sub for sub in group.commands}

    interaction = _admin_interaction(channel)
    await leaves["add"].callback(interaction, "daily@06:00 summarize")
    assert interaction.response.sent[0][1] is True
    (spec,) = actions.actions[_SCOPE]

    interaction = _admin_interaction(channel)
    await leaves["list"].callback(interaction)
    (listing, ephemeral) = interaction.response.sent[0]
    assert ephemeral is True
    assert spec.action_id in listing

    interaction = _admin_interaction(channel)
    await leaves["remove"].callback(interaction, spec.action_id)
    assert interaction.response.sent == [(cmd.REPLY_ACTION_REMOVED.format(id=spec.action_id), True)]


# --- anonymous /log via the message context menu (issue #44) -----------------


def _log_gateway() -> tuple[DiscordGateway, InMemoryProfiles, FakePublisher]:
    store = InMemoryProfiles()
    publisher = FakePublisher()
    directory = _directory(store)
    groups = GroupPolicy(allowed=set())
    engine = ActionEngine(
        sources={},
        transforms={
            "log_publish": LogPublishTransform(
                service=LogPublicationService(
                    publisher=publisher,
                    sanitizer=PassthroughSanitizer(),
                    pseudonyms=SequentialPseudonyms(),
                    clock=FakeClock(),
                    target_page="Project:Log",
                    edit_summary="Log entry",
                    timestamp_granularity=TimestampGranularity.NONE,
                ),
                page_url_for=str,
            )
        },
        sinks={"chat_confirm": ChatConfirmSink()},
        counters=Counters(),
        clock=FakeClock(),
    )
    commands = CommandService(
        directory=directory,
        groups=groups,
        page_url_for=str,
        counters=Counters(),
        engine=engine,
        repo_limiter=SlidingWindowLimiter(clock=FakeClock(), limit=10, window=timedelta(minutes=1)),
    )
    gateway = DiscordGateway(directory=directory, groups=groups, commands=commands)
    return gateway, store, publisher


async def test_log_command_publishes_the_target_text() -> None:
    gateway, _store, publisher = _log_gateway()
    await gateway.directory.set_log_page(_SCOPE, "WikiProject Foo")
    reply = await gateway.log_command(_CHANNEL, None, text="worth keeping", is_author=True)
    assert "WikiProject Foo" in reply
    assert "worth keeping" in publisher.started[0][2]


async def test_log_command_honours_author_only_consent() -> None:
    gateway, _store, publisher = _log_gateway()
    await gateway.directory.set_log_page(_SCOPE, "WikiProject Foo")
    await gateway.directory.set_consent(_SCOPE, ConsentMode.AUTHOR_ONLY)
    reply = await gateway.log_command(_CHANNEL, None, text="someone else's", is_author=False)
    assert reply == cmd.REPLY_LOG_AUTHOR_ONLY
    assert publisher.wrote_nothing


async def test_log_context_menu_answers_ephemerally_and_compares_authors() -> None:
    """The requester stays unattributed: a context menu posts no message at
    all, so unlike Telegram there is nothing to delete afterwards."""
    gateway, _store, publisher = _log_gateway()
    await gateway.directory.set_log_page(_SCOPE, "WikiProject Foo")
    client = build_gateway_client(gateway)
    (menu,) = [
        c for c in client.tree.get_commands() if isinstance(c, discord.app_commands.ContextMenu)
    ]

    interaction = _admin_interaction(SimpleNamespace(id=_CHANNEL))
    interaction.user = SimpleNamespace(id=7, guild_permissions=None)
    target = SimpleNamespace(content="published verbatim", author=SimpleNamespace(id=7))
    await menu.callback(interaction, cast("Any", target))
    (content, ephemeral) = interaction.response.sent[0]
    assert ephemeral is True
    assert "WikiProject Foo" in content
    assert "published verbatim" in publisher.started[0][2]

    # A different author is refused under author_only consent.
    await gateway.directory.set_consent(_SCOPE, ConsentMode.AUTHOR_ONLY)
    interaction = _admin_interaction(SimpleNamespace(id=_CHANNEL))
    interaction.user = SimpleNamespace(id=7, guild_permissions=None)
    other = SimpleNamespace(content="not mine", author=SimpleNamespace(id=99))
    await menu.callback(interaction, cast("Any", other))
    assert interaction.response.sent == [(cmd.REPLY_LOG_AUTHOR_ONLY, True)]


# --- capture consent: the announcement must be loud (issue #17) ---------------


async def test_capture_on_announces_publicly_but_refuses_privately() -> None:
    """Discord has no privacy mode, so /capture on's confirmation IS the
    channel's notice that archiving began. Ephemeral would mean only the admin
    who ran it ever knew — and ephemeral messages are not permanent either."""
    gateway, _store, _archive, _capture = _capture_gateway()
    client = build_gateway_client(gateway)
    channel = SimpleNamespace(id=_CHANNEL)

    interaction = _admin_interaction(channel)
    await _command(client, "capture").callback(interaction, "on")
    (text, ephemeral) = interaction.response.sent[0]
    assert ephemeral is False  # the whole point
    assert text == cmd.REPLY_CAPTURE_ENABLED

    # Turning it back off is also public: members were told it started, so
    # they are told it stopped.
    interaction = _admin_interaction(channel)
    await _command(client, "capture").callback(interaction, "off")
    assert interaction.response.sent == [(cmd.REPLY_CAPTURE_DISABLED, False)]

    # A refusal is nobody else's business.
    outsider = SimpleNamespace(
        channel=channel,
        user=SimpleNamespace(guild_permissions=SimpleNamespace(administrator=False)),
        response=_Response(),
    )
    await _command(client, "capture").callback(cast("Any", outsider), "on")
    assert outsider.response.sent == [(cmd.REPLY_NOT_ADMIN, True)]


async def test_capture_is_off_until_an_admin_turns_it_on() -> None:
    """Adding the bot must archive nothing: the Message Content Intent means it
    already SEES every message, so the store default is the only gate."""
    gateway, store, archive, _capture = _capture_gateway()
    await gateway.ingest_message(
        channel_id=_CHANNEL,
        thread_id=None,
        author_id=7,
        message_id=1,
        posted_at=datetime(2026, 7, 20, tzinfo=UTC),
        text="said before anyone opted in",
        reply_to=None,
    )
    assert archive.messages == []
    assert store.profiles == {}  # no row at all, not merely a false flag


async def test_nothing_is_archived_before_the_announcement_is_sent() -> None:
    """Ordering matters: the announcement must not trail the first archived
    message. /capture on writes the flag and returns the notice together, so a
    message arriving after the toggle is the earliest that can be captured."""
    gateway, _store, archive, _capture = _capture_gateway()
    client = build_gateway_client(gateway)
    interaction = _admin_interaction(SimpleNamespace(id=_CHANNEL))

    await _command(client, "capture").callback(interaction, "on")
    assert archive.messages == []  # nothing retroactive
    assert interaction.response.sent[0][1] is False  # …and the channel was told

    await gateway.ingest_message(
        channel_id=_CHANNEL,
        thread_id=None,
        author_id=7,
        message_id=2,
        posted_at=datetime(2026, 7, 20, tzinfo=UTC),
        text="after the notice",
        reply_to=None,
    )
    assert [m.text for m in archive.messages] == ["after the notice"]


# --- DM transcription: /transcribe (issue #45) ---------------------------------


def _transcribe_gateway() -> tuple[DiscordGateway, InMemoryProfiles, FakePublisher]:
    store = InMemoryProfiles()
    publisher = FakePublisher()
    directory = _directory(store)
    groups = GroupPolicy(allowed=set())
    clock = FakeClock()
    gateway = _make_gateway(directory, groups)
    gateway.transcription = DmTranscriptionService(
        publisher=publisher,
        sanitizer=PassthroughSanitizer(),
        sessions=SessionRegistry(
            pseudonyms=SequentialPseudonyms(), clock=clock, ttl=timedelta(hours=1)
        ),
        target_page="Project:Discussions",
        edit_summary="DM",
        debounce_seconds=0,
        timestamp_granularity=TimestampGranularity.NONE,
    )
    gateway.routes = DmRouteRegistry(clock=clock, route_ttl=timedelta(hours=1))
    return gateway, store, publisher


_DM_ID = 987654


async def test_transcribe_needs_the_channel_to_have_chosen_a_page() -> None:
    gateway, _store, _publisher = _transcribe_gateway()
    assert await gateway.transcribe_command(_CHANNEL, None, _DM_ID) == cmd.REPLY_LOG_NO_PAGE
    # Unrouted DMs stay silent — the bot must not answer private messages it
    # was never asked to publish.
    assert await gateway.transcribe_dm(_DM_ID, "hello?") is None


async def test_transcribe_routes_the_dm_to_the_channels_page() -> None:
    """The destination is known because the flow STARTED in the channel — no
    picker, no deep link (Discord's bot_can_open_dm is True)."""
    gateway, _store, _publisher = _transcribe_gateway()
    await gateway.directory.set_log_page(_SCOPE, "WikiProject Foo")

    opened = await gateway.transcribe_command(_CHANNEL, None, _DM_ID)
    assert "WikiProject Foo/Discord logs" in opened
    assert gateway.routes is not None
    route = gateway.routes.route_for(dm_scope(_DM_ID))
    assert route is not None
    assert route.scope == _SCOPE  # the channel it was invoked in


async def test_ten_dm_messages_become_one_discussion() -> None:
    """The whole point of the feature: many messages, one section, one pseudonym."""
    gateway, _store, publisher = _transcribe_gateway()
    await gateway.directory.set_log_page(_SCOPE, "WikiProject Foo")
    await gateway.transcribe_command(_CHANNEL, None, _DM_ID)

    notices = [await gateway.transcribe_dm(_DM_ID, f"point {n}") for n in range(1, 11)]

    # Exactly one notice — the first, disclosing the pseudonym; then silence.
    assert notices[0] is not None
    assert "WikiProject Foo/Discord logs" in notices[0]
    assert notices[1:] == [None] * 9

    # One section started, the other nine lines appended into it.
    assert len(publisher.started) == 1
    assert len(publisher.continued) == 9
    pages = {page for page, _heading, _text, _summary in publisher.started + publisher.continued}
    assert pages == {"WikiProject Foo/Discord logs"}
    headings = {h for _page, h, _text, _summary in publisher.started + publisher.continued}
    assert len(headings) == 1  # one heading = one discussion
    published = " ".join(text for _p, _h, text, _s in publisher.started + publisher.continued)
    for n in range(1, 11):
        assert f"point {n}" in published


async def test_transcribe_refuses_an_unserved_channel_and_reports_a_wiki_failure() -> None:
    gateway, _store, _publisher = _transcribe_gateway()
    gateway.groups = GroupPolicy(allowed={"999"})
    assert await gateway.transcribe_command(_CHANNEL, None, _DM_ID) == cmd.REPLY_NOT_ALLOWED

    broken, _store2, _pub = _transcribe_gateway()
    broken.transcription = cast("Any", _FailingTranscription())
    await broken.directory.set_log_page(_SCOPE, "WikiProject Foo")
    await broken.transcribe_command(_CHANNEL, None, _DM_ID)
    assert await broken.transcribe_dm(_DM_ID, "x") == gw.REPLY_TRANSCRIBE_FAILED


async def test_transcribe_is_unavailable_without_the_wiring() -> None:
    gateway, _store, _publisher = _transcribe_gateway()
    gateway.transcription = None
    assert (
        await gateway.transcribe_command(_CHANNEL, None, _DM_ID) == gw.REPLY_TRANSCRIBE_UNAVAILABLE
    )
    assert await gateway.transcribe_dm(_DM_ID, "x") is None


async def test_transcribe_fails_closed_when_channel_settings_are_unknown() -> None:
    gateway, _store, _publisher = _transcribe_gateway()
    gateway.directory.store = InMemoryProfiles(fail=True)
    assert (
        await gateway.transcribe_command(_CHANNEL, None, _DM_ID) == cmd.REPLY_LOG_CONFIG_UNAVAILABLE
    )


async def test_a_dm_never_reaches_capture_ingestion() -> None:
    """Capture is per-channel and admin-announced; a DM is never a candidate."""
    store = InMemoryProfiles(profiles={_SCOPE: GroupProfile(scope=_SCOPE, capture_enabled=True)})
    gateway, _store, archive, _capture = _capture_gateway(store=store)
    client = build_gateway_client(gateway)
    sent: list[str] = []
    dm_message = SimpleNamespace(
        author=SimpleNamespace(bot=False, id=7),
        guild=None,  # a DM
        channel=SimpleNamespace(id=_DM_ID, send=_recorder(sent)),
        id=42,
        created_at=datetime(2026, 7, 20, tzinfo=UTC),
        content="private",
        reference=None,
    )
    await client.on_message(cast("discord.Message", dm_message))
    assert archive.messages == []  # not archived…
    assert sent == []  # …and no reply, since it is unrouted


def _recorder(sink: list[str]) -> Any:
    async def send(text: str) -> None:
        sink.append(text)

    return send


class _FailingTranscription:
    """A transcription service whose wiki write always fails."""

    def __init__(self) -> None:
        self.sessions = SessionRegistry(
            pseudonyms=SequentialPseudonyms(), clock=FakeClock(), ttl=timedelta(hours=1)
        )

    async def record(self, scope: object, text: str, target_page: str | None = None) -> object:
        del scope, text, target_page
        raise WikiWriteError


async def test_transcribe_slash_command_opens_the_dm_and_answers_ephemerally() -> None:
    gateway, _store, _publisher = _transcribe_gateway()
    await gateway.directory.set_log_page(_SCOPE, "WikiProject Foo")
    client = build_gateway_client(gateway)
    interaction = _dm_interaction(SimpleNamespace(id=_CHANNEL), dm_id=_DM_ID)

    await _command(client, "transcribe").callback(interaction)

    (content, ephemeral) = interaction.response.sent[0]
    # Which channel someone is about to write privately about is nobody
    # else's business, so this one stays ephemeral.
    assert ephemeral is True
    assert "WikiProject Foo/Discord logs" in content
    assert gateway.routes is not None
    assert gateway.routes.route_for(dm_scope(_DM_ID)) is not None


async def test_a_routed_dm_is_transcribed_through_the_client_shell() -> None:
    gateway, _store, publisher = _transcribe_gateway()
    await gateway.directory.set_log_page(_SCOPE, "WikiProject Foo")
    await gateway.transcribe_command(_CHANNEL, None, _DM_ID)
    client = build_gateway_client(gateway)
    sent: list[str] = []
    dm_message = SimpleNamespace(
        author=SimpleNamespace(bot=False, id=7),
        guild=None,
        channel=SimpleNamespace(id=_DM_ID, send=_recorder(sent)),
        id=42,
        created_at=datetime(2026, 7, 20, tzinfo=UTC),
        content="first point",
        reference=None,
    )
    await client.on_message(cast("discord.Message", dm_message))
    assert len(sent) == 1  # the pseudonym disclosure
    assert "first point" in publisher.started[0][2]


async def test_subscribe_reports_a_storage_failure_while_minting_the_code() -> None:
    """The subscribable-code mint is Discord's own pre-step; its outage is
    reported before the neutral service is ever reached."""
    gateway, store, _archive, _capture = _capture_gateway()
    subs = InMemorySubscriptions()
    gateway.commands.subscriptions = subs
    gateway.commands.capabilities = DISCORD_CAPABILITIES
    store.fail_upserts = True
    assert await gateway.subscribe_command(_CHANNEL, None, 321, "") == gw.REPLY_STORAGE_DOWN
    assert subs.subs == {}


# --- bridge relay (#79) -------------------------------------------------------


class _RecordingRouter:
    def __init__(self) -> None:
        self.dispatched: list[Any] = []

    async def dispatch(self, message: Any) -> None:
        self.dispatched.append(message)


def _relayable(**extra: Any) -> Any:
    base: dict[str, Any] = {
        "author": SimpleNamespace(bot=False, id=7, display_name="Alice"),
        "guild": SimpleNamespace(id=1),
        "channel": SimpleNamespace(id=_CHANNEL),
        "id": 42,
        "created_at": datetime(2026, 7, 20, tzinfo=UTC),
        "content": "hi there",
        "reference": None,
        "attachments": [],
    }
    base.update(extra)
    return SimpleNamespace(**base)


async def test_a_message_is_relayed_by_name_and_archived_pseudonymously() -> None:
    """The two paths read the same message and never see each other's data."""
    store = InMemoryProfiles(profiles={_SCOPE: GroupProfile(scope=_SCOPE, capture_enabled=True)})
    gateway, _store, archive, _capture = _capture_gateway(store=store)
    router = _RecordingRouter()
    client = build_gateway_client(gateway, bridge=cast("Any", router))

    await client.on_message(cast("discord.Message", _relayable()))

    (relayed,) = router.dispatched
    assert (relayed.author, relayed.text) == ("Alice", "hi there")  # the real name
    (stored,) = archive.messages
    assert stored.text == "hi there"
    assert "Alice" not in stored.author  # ...the archive only ever sees the label


async def test_an_attachment_only_message_relays_as_a_marker() -> None:
    gateway, _store, _archive, _capture = _capture_gateway()
    router = _RecordingRouter()
    client = build_gateway_client(gateway, bridge=cast("Any", router))
    attached = _relayable(content="", attachments=[SimpleNamespace(filename="notes.pdf")])

    await client.on_message(cast("discord.Message", attached))

    assert router.dispatched[0].text == "[file: notes.pdf]"


async def test_a_message_with_nothing_to_mirror_is_not_relayed() -> None:
    gateway, _store, _archive, _capture = _capture_gateway()
    router = _RecordingRouter()
    client = build_gateway_client(gateway, bridge=cast("Any", router))

    await client.on_message(cast("discord.Message", _relayable(content="")))

    assert router.dispatched == []


async def test_without_a_bridge_the_relay_is_simply_skipped() -> None:
    gateway, _store, _archive, _capture = _capture_gateway()
    client = build_gateway_client(gateway)  # no bridge wired
    await client.on_message(cast("discord.Message", _relayable()))  # must not raise


async def test_bridge_slash_command_answers_the_channel_not_just_the_caller() -> None:
    """Joining changes who can read this channel, so the channel is the
    audience — the same reason /capture announces (§22.5)."""
    gateway, _store, _archive, _capture = _capture_gateway()
    client = build_gateway_client(gateway)
    channel = SimpleNamespace(id=_CHANNEL)
    interaction = _admin_interaction(channel)

    await _command(client, "bridge").callback(interaction, "new")

    (text, ephemeral) = interaction.response.sent[0]
    assert ephemeral is False
    assert text == cmd.REPLY_BRIDGE_OFF_DEPLOY  # nothing could relay here


async def test_a_non_admin_bridge_attempt_is_refused_by_the_service() -> None:
    gateway, _store, _archive, _capture = _capture_gateway()
    reply = await gateway.bridge_command(_CHANNEL, None, is_admin=False, tokens=["new"])
    assert reply == cmd.REPLY_NOT_ADMIN


async def test_setconsent_and_subscribable_reach_the_neutral_service() -> None:
    store = InMemoryProfiles()
    gateway = _make_gateway(_directory(store), GroupPolicy(allowed=set()))

    assert await gateway.setconsent_command(
        _CHANNEL, None, is_admin=True, mode="author_only"
    ) == cmd.REPLY_CONSENT_SET.format(mode="author_only")
    assert store.profiles[_SCOPE].consent_mode is ConsentMode.AUTHOR_ONLY

    opened = await gateway.subscribable_command(_CHANNEL, None, is_admin=True, enabled=True)
    assert opened == cmd.REPLY_SUBSCRIBABLE_ON
    assert store.profiles[_SCOPE].subscribe_code  # minted, not shown: no deep links


async def test_subscribable_slash_validates_its_state_argument() -> None:
    gateway = _make_gateway(_directory(InMemoryProfiles()), GroupPolicy(allowed=set()))
    client = build_gateway_client(gateway)
    interaction = _admin_interaction(SimpleNamespace(id=_CHANNEL))

    await _command(client, "subscribable").callback(interaction, "maybe")

    assert interaction.response.sent[0][0] == gw.SUBSCRIBABLE_USAGE


async def test_setconsent_slash_answers_privately_but_subscribable_publicly() -> None:
    """Opening a channel to digests changes who can read it away from the
    channel, so the channel is the audience — as with /capture."""
    gateway = _make_gateway(_directory(InMemoryProfiles()), GroupPolicy(allowed=set()))
    client = build_gateway_client(gateway)

    private = _admin_interaction(SimpleNamespace(id=_CHANNEL))
    await _command(client, "setconsent").callback(private, "author_only")
    assert private.response.sent[0][1] is True  # ephemeral

    public = _admin_interaction(SimpleNamespace(id=_CHANNEL))
    await _command(client, "subscribable").callback(public, "on")
    assert public.response.sent[0][1] is False
