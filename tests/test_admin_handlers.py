"""AdminHandlers tests: live admin gate, self-service commands (spec v2)."""

from __future__ import annotations

from types import SimpleNamespace
from typing import TYPE_CHECKING

from telegram import Update
from telegram.constants import ChatMemberStatus
from telegram.error import TelegramError
from telegram.ext import ContextTypes

from blybot.adapters.telegram import admin as a
from blybot.domain.models import ConsentMode
from blybot.observability import Counters
from blybot.services.directory import ChannelDirectory
from tests import tg
from tests.fakes import InMemoryProfiles

if TYPE_CHECKING:
    from unittest.mock import AsyncMock


def make_handlers(
    store: InMemoryProfiles | None = None,
    page_prefix: str = "Telegram logs/",
) -> a.AdminHandlers:
    return a.AdminHandlers(
        directory=ChannelDirectory(
            store=store if store is not None else InMemoryProfiles(),
            default_log_page="Next 25/Telegram logs",
            default_consent=ConsentMode.IMMEDIATE,
            default_repo="",
            page_prefix=page_prefix,
        ),
        counters=Counters(),
        page_url_for=lambda title: f"https://meta.wikimedia.org/wiki/{title.replace(' ', '_')}",
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
    for name in ("on_setup", "on_setpage", "on_setconsent", "on_settings", "on_reset"):
        context, bot = admin_context(status=ChatMemberStatus.MEMBER)
        await getattr(handlers, name)(command("/x"), context)
        assert tg.sent_texts(bot) == [a.REPLY_NOT_ADMIN]


async def test_admin_commands_outside_groups_are_ignored() -> None:
    handlers = make_handlers()
    context, bot = admin_context()
    private = tg.command_update(tg.message(chat=tg.PRIVATE, text="/setup"))
    await handlers.on_setup(private, context)
    assert tg.sent_texts(bot) == []


async def test_self_service_off_is_announced_to_admins() -> None:
    handlers = a.AdminHandlers(
        directory=ChannelDirectory(
            store=None,
            default_log_page="P",
            default_consent=ConsentMode.IMMEDIATE,
            default_repo="",
            page_prefix="",
        ),
        counters=Counters(),
        page_url_for=str,
    )
    context, bot = admin_context()
    await handlers.on_setup(command("/setup"), context)
    assert tg.sent_texts(bot) == [a.REPLY_SELF_SERVICE_OFF]


async def test_setup_lists_the_commands() -> None:
    handlers = make_handlers()
    context, bot = admin_context()
    await handlers.on_setup(command("/setup"), context)
    (sent,) = tg.sent_texts(bot)
    for expected in ("/setpage", "/setconsent", "/settings", "/reset", "Telegram logs/"):
        assert expected in sent


async def test_setpage_stores_and_links_the_page() -> None:
    store = InMemoryProfiles()
    handlers = make_handlers(store)
    context, bot = admin_context(args=["Telegram", "logs/Ours"])
    await handlers.on_setpage(command("/setpage Telegram logs/Ours"), context)

    assert store.profiles[tg.GROUP.id].log_page == "Telegram logs/Ours"
    (sent,) = tg.sent_texts(bot)
    assert sent == a.REPLY_PAGE_SET.format(url="https://meta.wikimedia.org/wiki/Telegram_logs/Ours")


async def test_setpage_without_arguments_shows_usage() -> None:
    handlers = make_handlers()
    context, bot = admin_context()
    await handlers.on_setpage(command("/setpage"), context)
    assert tg.sent_texts(bot) == [a.REPLY_SETPAGE_USAGE.format(prefix="Telegram logs/")]


async def test_setpage_refuses_pages_outside_the_prefix() -> None:
    handlers = make_handlers()
    context, bot = admin_context(args=["User:Jimbo"])
    await handlers.on_setpage(command("/setpage User:Jimbo"), context)
    assert tg.sent_texts(bot) == [a.REPLY_PAGE_REFUSED.format(prefix="Telegram logs/")]


async def test_setpage_reports_disabled_page_targeting() -> None:
    handlers = make_handlers(page_prefix="")
    context, bot = admin_context(args=["anything"])
    await handlers.on_setpage(command("/setpage anything"), context)
    assert tg.sent_texts(bot) == [a.REPLY_SELF_SERVICE_OFF]


async def test_setpage_reports_storage_outage() -> None:
    handlers = make_handlers(InMemoryProfiles(fail=True))
    context, bot = admin_context(args=["Telegram", "logs/Ours"])
    await handlers.on_setpage(command("/setpage Telegram logs/Ours"), context)
    assert tg.sent_texts(bot) == [a.REPLY_STORAGE_DOWN]


async def test_setconsent_stores_the_policy() -> None:
    store = InMemoryProfiles()
    handlers = make_handlers(store)
    context, bot = admin_context(args=["author_only"])
    await handlers.on_setconsent(command("/setconsent author_only"), context)
    assert store.profiles[tg.GROUP.id].consent_mode is ConsentMode.AUTHOR_ONLY
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

    await handlers.directory.set_log_page(tg.GROUP.id, "Telegram logs/Ours")
    await handlers.on_settings(command("/settings"), context)
    latest = tg.sent_texts(bot)[-1]
    assert "(all defaults)" not in latest
    assert "Telegram_logs/Ours" in latest


async def test_reset_returns_the_group_to_defaults() -> None:
    store = InMemoryProfiles()
    handlers = make_handlers(store)
    context, bot = admin_context()
    await handlers.directory.set_log_page(tg.GROUP.id, "Telegram logs/Ours")
    await handlers.on_reset(command("/reset"), context)
    assert store.profiles == {}
    assert tg.sent_texts(bot)[-1] == a.REPLY_RESET


async def test_reset_reports_storage_outage() -> None:
    handlers = make_handlers(InMemoryProfiles(fail=True))
    context, bot = admin_context()
    await handlers.on_reset(command("/reset"), context)
    assert tg.sent_texts(bot) == [a.REPLY_STORAGE_DOWN]
