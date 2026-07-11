"""AdminHandlers tests: live admin gate, self-service commands (spec v2)."""

from __future__ import annotations

from types import SimpleNamespace
from typing import TYPE_CHECKING

from telegram import Update
from telegram.constants import ChatMemberStatus
from telegram.error import TelegramError
from telegram.ext import ContextTypes

from blybot.adapters.telegram import admin as a
from blybot.domain.models import ConsentMode, EventKind
from blybot.observability import Counters
from blybot.services.binding import TokenBinding
from blybot.services.directory import ChannelDirectory
from blybot.services.policy import GroupPolicy
from tests import tg
from tests.fakes import FakeClock, InMemoryProfiles

if TYPE_CHECKING:
    from unittest.mock import AsyncMock


def make_handlers(
    store: InMemoryProfiles | None = None,
    page_suffix: str = "Telegram logs",
) -> a.AdminHandlers:
    store = store if store is not None else InMemoryProfiles()
    return a.AdminHandlers(
        groups=GroupPolicy(allowed=set()),
        directory=ChannelDirectory(
            store=store,
            default_log_page="Next 25/Telegram logs",
            default_consent=ConsentMode.IMMEDIATE,
            default_repo="",
            page_suffix=page_suffix,
        ),
        counters=Counters(),
        page_url_for=lambda title: f"https://meta.wikimedia.org/wiki/{title.replace(' ', '_')}",
        binding=TokenBinding(clock=FakeClock()),
        vault=store,
    )


def admin_context(
    status: ChatMemberStatus = ChatMemberStatus.ADMINISTRATOR,
    args: list[str] | None = None,
) -> tuple[ContextTypes.DEFAULT_TYPE, AsyncMock]:
    context, bot = tg.make_context(args=args)
    bot.get_chat_member.return_value = SimpleNamespace(status=status)
    return context, bot


def command(text: str) -> Update:
    return tg.command_update(tg.message(text=text, from_user=tg.ALICE))


async def test_admin_check_is_performed_live() -> None:
    _context, bot = admin_context()
    assert await a.is_group_admin(bot, -1, 1)
    bot.get_chat_member.return_value = SimpleNamespace(status=ChatMemberStatus.MEMBER)
    assert not await a.is_group_admin(bot, -1, 1)
    bot.get_chat_member.side_effect = TelegramError("hidden")
    assert not await a.is_group_admin(bot, -1, 1)


async def test_non_admins_are_refused_on_every_command() -> None:
    handlers = make_handlers()
    for name in (
        "on_setup",
        "on_setpage",
        "on_setconsent",
        "on_setrepo",
        "on_events",
        "on_revoke",
        "on_settings",
        "on_reset",
    ):
        context, bot = admin_context(status=ChatMemberStatus.MEMBER)
        await getattr(handlers, name)(command("/x"), context)
        assert tg.sent_texts(bot) == [a.REPLY_NOT_ADMIN]


async def test_admin_commands_outside_groups_are_ignored() -> None:
    handlers = make_handlers()
    context, bot = admin_context()
    private = tg.command_update(tg.message(chat=tg.PRIVATE, text="/setup"))
    await handlers.on_setup(private, context)
    assert tg.sent_texts(bot) == []


async def test_v1_deployments_stay_silent_and_skip_the_api_call() -> None:
    handlers = a.AdminHandlers(
        groups=GroupPolicy(allowed=set()),
        directory=ChannelDirectory(
            store=None,
            default_log_page="P",
            default_consent=ConsentMode.IMMEDIATE,
            default_repo="",
            page_suffix="",
        ),
        counters=Counters(),
        page_url_for=str,
        binding=TokenBinding(clock=FakeClock()),
        vault=None,
    )
    context, bot = admin_context()
    await handlers.on_setup(command("/setup"), context)
    assert tg.sent_texts(bot) == []
    bot.get_chat_member.assert_not_awaited()  # no amplification on v1


async def test_unlisted_groups_cannot_configure_anything() -> None:
    handlers = make_handlers()
    handlers.groups.allowed = {-42}  # this test group is not on the list
    context, bot = admin_context(args=["WikiProject", "X"])
    await handlers.on_setpage(command("/setpage WikiProject X"), context)
    assert tg.sent_texts(bot) == []
    bot.get_chat_member.assert_not_awaited()


async def test_setup_lists_the_commands() -> None:
    handlers = make_handlers()
    context, bot = admin_context()
    await handlers.on_setup(command("/setup"), context)
    (sent,) = tg.sent_texts(bot)
    for expected in ("/setpage", "/setconsent", "/settings", "/reset", "Telegram logs"):
        assert expected in sent


async def test_setpage_stores_and_links_the_page() -> None:
    store = InMemoryProfiles()
    handlers = make_handlers(store)
    context, bot = admin_context(args=["WikiProject", "Ours"])
    await handlers.on_setpage(command("/setpage WikiProject Ours"), context)

    assert store.profiles[tg.GROUP.id, 0].log_page == "WikiProject Ours/Telegram logs"
    (sent,) = tg.sent_texts(bot)
    assert sent == a.REPLY_PAGE_SET.format(
        url="https://meta.wikimedia.org/wiki/WikiProject_Ours/Telegram_logs",
        scope="the group default",
    )


async def test_setpage_without_arguments_shows_usage() -> None:
    handlers = make_handlers()
    context, bot = admin_context()
    await handlers.on_setpage(command("/setpage"), context)
    assert tg.sent_texts(bot) == [a.REPLY_SETPAGE_USAGE.format(suffix="Telegram logs")]


async def test_setpage_refuses_invalid_base_paths() -> None:
    handlers = make_handlers()
    context, bot = admin_context(args=["bad", "{{title}}"])
    await handlers.on_setpage(command("/setpage bad {{title}}"), context)
    assert tg.sent_texts(bot) == [a.REPLY_PAGE_REFUSED.format(suffix="Telegram logs")]


async def test_setpage_reports_disabled_page_targeting() -> None:
    handlers = make_handlers(page_suffix="")
    context, bot = admin_context(args=["anything"])
    await handlers.on_setpage(command("/setpage anything"), context)
    assert tg.sent_texts(bot) == [a.REPLY_SELF_SERVICE_OFF]


async def test_setpage_reports_storage_outage() -> None:
    handlers = make_handlers(InMemoryProfiles(fail=True))
    context, bot = admin_context(args=["WikiProject", "Ours"])
    await handlers.on_setpage(command("/setpage WikiProject Ours"), context)
    assert tg.sent_texts(bot) == [a.REPLY_STORAGE_DOWN]


async def test_setconsent_stores_the_policy() -> None:
    store = InMemoryProfiles()
    handlers = make_handlers(store)
    context, bot = admin_context(args=["author_only"])
    await handlers.on_setconsent(command("/setconsent author_only"), context)
    assert store.profiles[tg.GROUP.id, 0].consent_mode is ConsentMode.AUTHOR_ONLY
    assert tg.sent_texts(bot) == [a.REPLY_CONSENT_SET.format(mode="author_only")]


async def test_setconsent_rejects_unknown_values_and_confirm() -> None:
    handlers = make_handlers()
    for bad in ([], ["confirm"], ["maybe"]):
        context, bot = admin_context(args=bad)
        await handlers.on_setconsent(command("/setconsent"), context)
        assert tg.sent_texts(bot) == [a.REPLY_CONSENT_USAGE]


async def test_setconsent_reports_storage_outage() -> None:
    handlers = make_handlers(InMemoryProfiles(fail=True))
    context, bot = admin_context(args=["immediate"])
    await handlers.on_setconsent(command("/setconsent immediate"), context)
    assert tg.sent_texts(bot) == [a.REPLY_STORAGE_DOWN]


async def test_settings_shows_defaults_then_customization() -> None:
    handlers = make_handlers()
    context, bot = admin_context()
    await handlers.on_settings(command("/settings"), context)
    assert "(all defaults)" in tg.sent_texts(bot)[0]
    assert "Next_25/Telegram_logs" in tg.sent_texts(bot)[0]

    await handlers.directory.set_log_page(tg.GROUP.id, 0, "WikiProject Ours")
    await handlers.on_settings(command("/settings"), context)
    latest = tg.sent_texts(bot)[-1]
    assert "(all defaults)" not in latest
    assert "WikiProject_Ours/Telegram_logs" in latest


async def test_reset_returns_the_group_to_defaults() -> None:
    store = InMemoryProfiles()
    handlers = make_handlers(store)
    context, bot = admin_context()
    await handlers.directory.set_log_page(tg.GROUP.id, 0, "WikiProject Ours")
    await handlers.on_reset(command("/reset"), context)
    assert store.profiles == {}
    assert tg.sent_texts(bot)[-1] == a.REPLY_RESET.format(scope="the group default")


async def test_reset_reports_storage_outage() -> None:
    handlers = make_handlers(InMemoryProfiles(fail=True))
    context, bot = admin_context()
    await handlers.on_reset(command("/reset"), context)
    assert tg.sent_texts(bot) == [a.REPLY_STORAGE_DOWN]


async def test_setrepo_binds_and_mints_a_deep_link() -> None:
    store = InMemoryProfiles()
    handlers = make_handlers(store)
    context, bot = admin_context(args=["wikimedia/mediawiki"])
    await handlers.on_setrepo(command("/setrepo wikimedia/mediawiki"), context)

    assert store.profiles[tg.GROUP.id, 0].repo == "wikimedia/mediawiki"
    (sent,) = tg.sent_texts(bot)
    assert "https://t.me/blybot_bot?start=cfg_" in sent
    assert "fine-grained" in sent


async def test_setrepo_rejects_bad_formats() -> None:
    handlers = make_handlers()
    for bad in ([], ["not-a-repo"], ["a/b/c"], ["owner/"], ["owner/.."], ["../x"]):
        context, bot = admin_context(args=bad)
        await handlers.on_setrepo(command("/setrepo"), context)
        assert tg.sent_texts(bot) == [a.REPLY_SETREPO_USAGE]


async def test_setrepo_reports_storage_outage() -> None:
    handlers = make_handlers(InMemoryProfiles(fail=True))
    context, bot = admin_context(args=["x/y"])
    await handlers.on_setrepo(command("/setrepo x/y"), context)
    assert tg.sent_texts(bot) == [a.REPLY_STORAGE_DOWN]


async def test_revoke_discards_the_token() -> None:
    store = InMemoryProfiles()
    handlers = make_handlers(store)
    await store.store_token(tg.GROUP.id, 0, "ghp_x")
    context, bot = admin_context()
    await handlers.on_revoke(command("/revoke"), context)
    assert store.tokens == {}
    assert tg.sent_texts(bot) == [a.REPLY_PAT_REVOKED]


async def test_revoke_reports_storage_outage() -> None:
    handlers = make_handlers(InMemoryProfiles(fail=True))
    context, bot = admin_context()
    await handlers.on_revoke(command("/revoke"), context)
    assert tg.sent_texts(bot) == [a.REPLY_STORAGE_DOWN]


async def test_revoke_without_a_vault_reports_self_service_off() -> None:
    handlers = make_handlers()
    handlers.vault = None  # store present but no vault: defensive wiring
    context, bot = admin_context()
    await handlers.on_revoke(command("/revoke"), context)
    assert tg.sent_texts(bot) == [a.REPLY_SELF_SERVICE_OFF]


async def test_events_on_uses_default_kinds() -> None:
    store = InMemoryProfiles()
    handlers = make_handlers(store)
    context, bot = admin_context(args=["on"])
    await handlers.on_events(command("/events on"), context)
    profile = store.profiles[tg.GROUP.id, 0]
    assert profile.events_enabled
    assert profile.event_kinds == a.DEFAULT_EVENT_KINDS
    assert tg.sent_texts(bot) == [a.REPLY_EVENTS_SET.format(state="prs, releases")]


async def test_events_accepts_explicit_kinds_and_off() -> None:
    store = InMemoryProfiles()
    handlers = make_handlers(store)
    context, _ = admin_context(args=["releases,issues"])
    await handlers.on_events(command("/events releases,issues"), context)
    assert store.profiles[tg.GROUP.id, 0].event_kinds == frozenset(
        {EventKind.RELEASES, EventKind.ISSUES}
    )

    context, bot = admin_context(args=["off"])
    await handlers.on_events(command("/events off"), context)
    assert not store.profiles[tg.GROUP.id, 0].events_enabled
    assert tg.sent_texts(bot) == [a.REPLY_EVENTS_SET.format(state="off")]


async def test_events_rejects_junk_and_reports_outages() -> None:
    handlers = make_handlers()
    for bad in ([], ["sometimes"], ["releases", "junk"]):
        context, bot = admin_context(args=bad)
        await handlers.on_events(command("/events"), context)
        assert tg.sent_texts(bot) == [a.REPLY_EVENTS_USAGE]

    handlers = make_handlers(InMemoryProfiles(fail=True))
    context, bot = admin_context(args=["on"])
    await handlers.on_events(command("/events on"), context)
    assert tg.sent_texts(bot) == [a.REPLY_STORAGE_DOWN]


async def test_setrepo_discards_any_previous_token() -> None:
    """A token consented for repo A must never be replayed against repo B."""
    store = InMemoryProfiles()
    handlers = make_handlers(store)
    await store.store_token(tg.GROUP.id, 0, "ghp_for_old_repo")
    context, _ = admin_context(args=["new/repo"])
    await handlers.on_setrepo(command("/setrepo new/repo"), context)
    assert store.tokens == {}


async def test_setrepo_without_a_vault_still_binds() -> None:
    store = InMemoryProfiles()
    handlers = make_handlers(store)
    handlers.vault = None
    context, bot = admin_context(args=["x/y"])
    await handlers.on_setrepo(command("/setrepo x/y"), context)
    assert store.profiles[tg.GROUP.id, 0].repo == "x/y"
    assert "cfg_" in tg.sent_texts(bot)[0]
