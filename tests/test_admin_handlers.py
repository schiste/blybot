"""AdminHandlers tests: live admin gate, self-service commands (spec v2)."""

from __future__ import annotations

from dataclasses import replace
from datetime import timedelta
from types import SimpleNamespace
from typing import TYPE_CHECKING

from telegram import Update
from telegram.constants import ChatMemberStatus
from telegram.error import TelegramError
from telegram.ext import ContextTypes

from blybot.adapters.telegram import admin as a
from blybot.domain.models import (
    CapturedMessage,
    ConsentMode,
    DeliveryMode,
    EventType,
    LlmSettings,
    Scope,
)
from blybot.observability import Counters
from blybot.services import commands as cmd
from blybot.services.actions import MAX_ACTIONS, parse_action
from blybot.services.binding import TokenBinding
from blybot.services.capture import CaptureService
from blybot.services.commands import CommandService
from blybot.services.directory import ChannelDirectory
from blybot.services.policy import GroupPolicy, SlidingWindowLimiter
from blybot.services.rules import MAX_RULES
from tests import tg
from tests.fakes import FakeClock, InMemoryActions, InMemoryArchive, InMemoryProfiles

if TYPE_CHECKING:
    from unittest.mock import AsyncMock


def gscope(chat_id: int = tg.GROUP.id, thread_id: int = 0) -> Scope:
    return Scope("telegram", str(chat_id), str(thread_id) if thread_id else "")


def _page_url(title: str) -> str:
    return f"https://meta.wikimedia.org/wiki/{title.replace(' ', '_')}"


def make_handlers(
    store: InMemoryProfiles | None = None,
    page_suffix: str = "Telegram logs",
) -> a.AdminHandlers:
    store = store if store is not None else InMemoryProfiles()
    groups = GroupPolicy(allowed=set())
    directory = ChannelDirectory(
        store=store,
        default_log_page="Next 25/Telegram logs",
        default_consent=ConsentMode.IMMEDIATE,
        default_repo="",
        page_suffix=page_suffix,
    )
    counters = Counters()
    return a.AdminHandlers(
        groups=groups,
        directory=directory,
        counters=counters,
        page_url_for=_page_url,
        binding=TokenBinding(clock=FakeClock()),
        vault=store,
        commands=CommandService(
            directory=directory,
            groups=groups,
            page_url_for=_page_url,
            counters=counters,
            capture_service=None,
            vault=store,
        ),
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


def command_in_topic(text: str, thread_id: int) -> Update:
    return tg.command_update(tg.message(text=text, from_user=tg.ALICE, thread_id=thread_id))


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
        "on_rule",
        "on_rules",
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
    groups = GroupPolicy(allowed=set())
    directory = ChannelDirectory(
        store=None,
        default_log_page="P",
        default_consent=ConsentMode.IMMEDIATE,
        default_repo="",
        page_suffix="",
    )
    counters = Counters()
    handlers = a.AdminHandlers(
        groups=groups,
        directory=directory,
        counters=counters,
        page_url_for=str,
        binding=TokenBinding(clock=FakeClock()),
        vault=None,
        commands=CommandService(
            directory=directory,
            groups=groups,
            page_url_for=str,
            counters=counters,
            capture_service=None,
        ),
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

    assert store.profiles[gscope()].log_page == "WikiProject Ours/Telegram logs"
    (sent,) = tg.sent_texts(bot)
    # Wording now comes from the neutral CommandService (no /log or scope label).
    assert sent == cmd.REPLY_PAGE_SET.format(
        url="https://meta.wikimedia.org/wiki/WikiProject_Ours/Telegram_logs",
    )


async def test_setpage_without_arguments_shows_usage() -> None:
    handlers = make_handlers()
    context, bot = admin_context()
    await handlers.on_setpage(command("/setpage"), context)
    assert tg.sent_texts(bot) == [cmd.REPLY_SETPAGE_USAGE.format(suffix="Telegram logs")]


async def test_setpage_refuses_invalid_base_paths() -> None:
    handlers = make_handlers()
    context, bot = admin_context(args=["bad", "{{title}}"])
    await handlers.on_setpage(command("/setpage bad {{title}}"), context)
    assert tg.sent_texts(bot) == [cmd.REPLY_PAGE_REFUSED.format(suffix="Telegram logs")]


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
    assert store.profiles[gscope()].consent_mode is ConsentMode.AUTHOR_ONLY
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

    await handlers.directory.set_log_page(gscope(), "WikiProject Ours")
    await handlers.on_settings(command("/settings"), context)
    latest = tg.sent_texts(bot)[-1]
    assert "(all defaults)" not in latest
    assert "WikiProject_Ours/Telegram_logs" in latest


async def test_reset_returns_the_group_to_defaults() -> None:
    store = InMemoryProfiles()
    handlers = make_handlers(store)
    context, bot = admin_context()
    await handlers.directory.set_log_page(gscope(), "WikiProject Ours")
    await handlers.on_reset(command("/reset"), context)
    assert store.profiles == {}
    assert tg.sent_texts(bot)[-1] == cmd.REPLY_RESET


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

    assert store.profiles[gscope()].repo == "wikimedia/mediawiki"
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
    await store.store_token(gscope(), "ghp_x")
    context, bot = admin_context()
    await handlers.on_revoke(command("/revoke"), context)
    assert store.tokens == {}
    assert tg.sent_texts(bot) == [cmd.REPLY_REVOKED]


async def test_revoke_reports_storage_outage() -> None:
    handlers = make_handlers(InMemoryProfiles(fail=True))
    context, bot = admin_context()
    await handlers.on_revoke(command("/revoke"), context)
    assert tg.sent_texts(bot) == [a.REPLY_STORAGE_DOWN]


async def test_revoke_without_a_vault_reports_self_service_off() -> None:
    handlers = make_handlers()
    handlers.commands.vault = None  # store present but no vault: defensive wiring
    context, bot = admin_context()
    await handlers.on_revoke(command("/revoke"), context)
    assert tg.sent_texts(bot) == [cmd.REPLY_SELF_SERVICE_OFF]


async def test_events_on_requires_a_repo_at_this_scope() -> None:
    store = InMemoryProfiles()
    handlers = make_handlers(store)
    context, bot = admin_context(args=["on"])
    await handlers.on_events(command("/events on"), context)  # no repo bound
    assert tg.sent_texts(bot) == [cmd.REPLY_EVENTS_NEED_REPO]
    assert gscope() not in store.profiles or not store.profiles[gscope()].events_enabled


async def test_events_on_enables_and_seeds_starter_rules() -> None:
    store = InMemoryProfiles()
    handlers = make_handlers(store)
    await handlers.directory.set_repo(gscope(), "org/repo")
    context, bot = admin_context(args=["on"])
    await handlers.on_events(command("/events on"), context)
    profile = store.profiles[gscope()]
    assert profile.events_enabled
    assert {rule.trigger.token for rule in profile.rules} == {"pr.merged", "release"}
    assert all(rule.mode is DeliveryMode.DIGEST for rule in profile.rules)
    assert tg.sent_texts(bot) == [cmd.REPLY_EVENTS_SEEDED]


async def test_events_on_keeps_existing_rules_and_does_not_reseed() -> None:
    store = InMemoryProfiles()
    handlers = make_handlers(store)
    await handlers.directory.set_repo(gscope(), "org/repo")
    context, _ = admin_context(args=["add", "issue.opened"])
    await handlers.on_rule(command("/rule add issue.opened"), context)
    context, bot = admin_context(args=["on"])
    await handlers.on_events(command("/events on"), context)
    rules = store.profiles[gscope()].rules
    assert [rule.trigger.token for rule in rules] == ["issue.opened"]  # not reseeded
    assert tg.sent_texts(bot) == [cmd.REPLY_EVENTS_SET.format(state="on")]


async def test_events_off_disables() -> None:
    store = InMemoryProfiles()
    handlers = make_handlers(store)
    await handlers.directory.set_repo(gscope(), "org/repo")
    context, _ = admin_context(args=["on"])
    await handlers.on_events(command("/events on"), context)
    context, bot = admin_context(args=["off"])
    await handlers.on_events(command("/events off"), context)
    assert not store.profiles[gscope()].events_enabled
    assert tg.sent_texts(bot) == [cmd.REPLY_EVENTS_SET.format(state="off")]


async def test_events_rejects_junk_and_reports_outages() -> None:
    handlers = make_handlers()
    for bad in ([], ["sometimes"], ["releases,prs"]):
        context, bot = admin_context(args=bad)
        await handlers.on_events(command("/events"), context)
        assert tg.sent_texts(bot) == [cmd.REPLY_EVENTS_USAGE]

    handlers = make_handlers(InMemoryProfiles(fail=True))
    context, bot = admin_context(args=["on"])
    await handlers.on_events(command("/events on"), context)
    assert tg.sent_texts(bot) == [a.REPLY_STORAGE_DOWN]


async def test_setrepo_discards_any_previous_token() -> None:
    """A token consented for repo A must never be replayed against repo B."""
    store = InMemoryProfiles()
    handlers = make_handlers(store)
    await store.store_token(gscope(), "ghp_for_old_repo")
    context, _ = admin_context(args=["new/repo"])
    await handlers.on_setrepo(command("/setrepo new/repo"), context)
    assert store.tokens == {}


async def test_setrepo_without_a_vault_still_binds() -> None:
    store = InMemoryProfiles()
    handlers = make_handlers(store)
    handlers.vault = None
    context, bot = admin_context(args=["x/y"])
    await handlers.on_setrepo(command("/setrepo x/y"), context)
    assert store.profiles[gscope()].repo == "x/y"
    assert "cfg_" in tg.sent_texts(bot)[0]


async def test_commands_configure_the_topic_they_are_run_in() -> None:
    store = InMemoryProfiles()
    handlers = make_handlers(store)
    context, bot = admin_context(args=["WikiProject", "Foo"])
    await handlers.on_setpage(command_in_topic("/setpage WikiProject Foo", 42), context)

    assert gscope(tg.GROUP.id, 42) in store.profiles  # the topic, not the group default
    assert store.profiles[gscope(tg.GROUP.id, 42)].log_page == "WikiProject Foo/Telegram logs"
    (sent, thread) = tg.sent_calls(bot)[0]
    assert thread == 42  # confirmation routed back into the topic
    assert "WikiProject_Foo/Telegram_logs" in sent  # neutral confirmation links the page


async def test_setconsent_says_group_wide_even_from_a_topic() -> None:
    store = InMemoryProfiles()
    handlers = make_handlers(store)
    context, bot = admin_context(args=["author_only"])
    await handlers.on_setconsent(command_in_topic("/setconsent author_only", 42), context)
    assert store.profiles[gscope()].consent_mode is ConsentMode.AUTHOR_ONLY  # thread 0
    assert gscope(tg.GROUP.id, 42) not in store.profiles
    assert "group-wide" in tg.sent_texts(bot)[0]


async def test_events_storage_failure_after_repo_check_is_reported() -> None:
    store = InMemoryProfiles()
    handlers = make_handlers(store)
    await handlers.directory.set_repo(gscope(), "org/repo")
    store.fail_upserts = True  # get() still works; the set_events write fails
    context, bot = admin_context(args=["on"])
    await handlers.on_events(command("/events on"), context)
    assert tg.sent_texts(bot) == [a.REPLY_STORAGE_DOWN]


async def test_events_profile_lookup_outage_is_reported() -> None:
    handlers = make_handlers(InMemoryProfiles(fail=True))
    context, bot = admin_context(args=["on"])
    await handlers.on_events(command("/events on"), context)
    assert tg.sent_texts(bot) == [a.REPLY_STORAGE_DOWN]


async def test_rule_add_stores_and_confirms() -> None:
    store = InMemoryProfiles()
    handlers = make_handlers(store)
    context, bot = admin_context(args=["add", "pr.merged", "base:main", "digest"])
    await handlers.on_rule(command("/rule add pr.merged base:main digest"), context)
    (rule,) = store.profiles[gscope()].rules
    assert rule.trigger is EventType.PR_MERGED
    assert rule.filter.base == "main"
    (sent,) = tg.sent_texts(bot)
    assert sent.startswith(f"Rule added for this scope: [{rule.rule_id}] pr.merged")


async def test_rules_lists_every_rule_with_ids() -> None:
    store = InMemoryProfiles()
    handlers = make_handlers(store)
    for spec in (["add", "pr.merged"], ["add", "issue.opened", "label:bug"]):
        context, _ = admin_context(args=spec)
        await handlers.on_rule(command("/rule"), context)
    context, bot = admin_context()
    await handlers.on_rules(command("/rules"), context)
    listing = tg.sent_texts(bot)[-1]
    assert listing.startswith("Rules for this scope:")
    assert "pr.merged" in listing
    assert "issue.opened label:bug → live" in listing


async def test_rules_empty_prompts_to_add_one() -> None:
    handlers = make_handlers()
    context, bot = admin_context()
    await handlers.on_rules(command("/rules"), context)
    assert tg.sent_texts(bot) == [cmd.REPLY_RULES_NONE.format()]


async def test_rule_remove_by_id_and_unknown() -> None:
    store = InMemoryProfiles()
    handlers = make_handlers(store)
    context, _ = admin_context(args=["add", "release"])
    await handlers.on_rule(command("/rule add release"), context)
    (rule,) = store.profiles[gscope()].rules

    context, bot = admin_context(args=["remove", "nope"])
    await handlers.on_rule(command("/rule remove nope"), context)
    assert tg.sent_texts(bot) == [cmd.REPLY_RULE_UNKNOWN.format(id="nope")]

    context, bot = admin_context(args=["remove", rule.rule_id])
    await handlers.on_rule(command("/rule remove id"), context)
    assert store.profiles[gscope()].rules == ()
    assert tg.sent_texts(bot) == [cmd.REPLY_RULE_REMOVED.format(id=rule.rule_id)]


async def test_rule_clear_reports_count() -> None:
    store = InMemoryProfiles()
    handlers = make_handlers(store)
    for spec in (["add", "pr.merged"], ["add", "release"]):
        context, _ = admin_context(args=spec)
        await handlers.on_rule(command("/rule"), context)
    context, bot = admin_context(args=["clear"])
    await handlers.on_rule(command("/rule clear"), context)
    assert store.profiles[gscope()].rules == ()
    assert tg.sent_texts(bot) == [cmd.REPLY_RULES_CLEARED.format(count=2)]


async def test_rule_add_surfaces_parse_errors() -> None:
    handlers = make_handlers()
    context, bot = admin_context(args=["add", "nope.nope"])
    await handlers.on_rule(command("/rule add nope.nope"), context)
    assert "Unknown event type" in tg.sent_texts(bot)[0]


async def test_rule_bad_subcommand_shows_usage() -> None:
    handlers = make_handlers()
    for args in ([], ["frobnicate"], ["remove"], ["remove", "a", "b"]):
        context, bot = admin_context(args=args)
        await handlers.on_rule(command("/rule"), context)
        assert tg.sent_texts(bot) == [cmd.REPLY_RULE_USAGE]


async def test_rule_cap_is_enforced() -> None:
    store = InMemoryProfiles()
    handlers = make_handlers(store)
    for _ in range(MAX_RULES):
        context, _ = admin_context(args=["add", "pr.merged"])
        await handlers.on_rule(command("/rule add pr.merged"), context)
    context, bot = admin_context(args=["add", "release"])
    await handlers.on_rule(command("/rule add release"), context)
    assert len(store.profiles[gscope()].rules) == MAX_RULES  # not exceeded
    assert tg.sent_texts(bot) == [cmd.REPLY_RULES_FULL.format(max=MAX_RULES)]


async def test_rule_and_rules_report_storage_outage() -> None:
    handlers = make_handlers(InMemoryProfiles(fail=True))
    context, bot = admin_context(args=["add", "pr.merged"])
    await handlers.on_rule(command("/rule add pr.merged"), context)
    assert tg.sent_texts(bot) == [a.REPLY_STORAGE_DOWN]

    context, bot = admin_context()
    await handlers.on_rules(command("/rules"), context)
    assert tg.sent_texts(bot) == [a.REPLY_STORAGE_DOWN]


async def test_rules_configure_the_topic_they_run_in() -> None:
    store = InMemoryProfiles()
    handlers = make_handlers(store)
    context, bot = admin_context(args=["add", "pr.merged"])
    await handlers.on_rule(command_in_topic("/rule add pr.merged", 42), context)
    assert store.profiles[gscope(tg.GROUP.id, 42)].rules  # the topic, not the group default
    (_sent, thread) = tg.sent_calls(bot)[0]
    assert thread == 42  # and the confirmation lands back in that topic
    assert "pr.merged" in tg.sent_texts(bot)[0]


def make_capture_handlers(
    store: InMemoryProfiles | None = None,
    archive: InMemoryArchive | None = None,
) -> tuple[a.AdminHandlers, InMemoryProfiles, InMemoryArchive]:
    store = store if store is not None else InMemoryProfiles()
    archive = archive if archive is not None else InMemoryArchive()
    handlers = make_handlers(store)
    clock = FakeClock()
    handlers.archive = archive
    service = CaptureService(
        store=store,
        archive=archive,
        limiter=SlidingWindowLimiter(clock=clock, limit=100, window=timedelta(minutes=1)),
        clock=clock,
        counters=Counters(),
        max_chars=4096,
    )
    handlers.capture_service = service
    # The shared CommandService owns the /capture on|off logic, so it must see
    # the same capture service the handler's purge branch uses.
    handlers.commands.capture_service = service
    return handlers, store, archive


async def test_capture_without_deployment_support_says_so() -> None:
    handlers = make_handlers()  # archive/capture_service left unset
    context, bot = admin_context(args=["on"])
    await handlers.on_capture(command("/capture on"), context)
    assert tg.sent_texts(bot) == [a.REPLY_CAPTURE_OFF_DEPLOY]


async def test_capture_usage_on_bad_arguments() -> None:
    handlers, _store, _archive = make_capture_handlers()
    context, bot = admin_context(args=["maybe"])
    await handlers.on_capture(command("/capture maybe"), context)
    assert tg.sent_texts(bot) == [a.REPLY_CAPTURE_USAGE]


async def test_capture_on_enables_and_announces_permanently() -> None:
    handlers, store, _archive = make_capture_handlers()
    context, bot = admin_context(args=["on"])

    await handlers.on_capture(command("/capture on"), context)

    assert store.profiles[gscope()].capture_enabled
    (announcement,) = tg.sent_texts(bot)
    assert "archived" in announcement
    assert "/capture off" in announcement  # the off-ramp is in the announcement


async def test_capture_off_keeps_the_archive() -> None:
    handlers, store, archive = make_capture_handlers()
    archive.messages.append(
        CapturedMessage(scope=gscope(), message_id=1, posted_at=tg.NOW, author="x")
    )
    context, bot = admin_context(args=["off"])

    await handlers.on_capture(command("/capture off"), context)

    assert not store.profiles[gscope()].capture_enabled
    assert len(archive.messages) == 1  # kept until an explicit purge
    # Neutral shared text, with Telegram re-appending its own /capture purge hint.
    assert tg.sent_texts(bot)[0] == f"{cmd.REPLY_CAPTURE_DISABLED} {a.REPLY_CAPTURE_PURGE_HINT}"


async def test_capture_purge_erases_the_scope_archive() -> None:
    handlers, _store, archive = make_capture_handlers()
    archive.messages.append(
        CapturedMessage(scope=gscope(), message_id=1, posted_at=tg.NOW, author="x")
    )
    context, bot = admin_context(args=["purge"])

    await handlers.on_capture(command("/capture purge"), context)

    assert archive.messages == []
    assert "1 message(s) deleted" in tg.sent_texts(bot)[0]


async def test_capture_purge_without_deployment_support_says_so() -> None:
    handlers = make_handlers()  # archive/capture_service left unset
    context, bot = admin_context(args=["purge"])
    await handlers.on_capture(command("/capture purge"), context)
    assert tg.sent_texts(bot) == [a.REPLY_CAPTURE_OFF_DEPLOY]


async def test_capture_purge_reports_storage_outage() -> None:
    handlers, _store, _archive = make_capture_handlers(archive=InMemoryArchive(fail=True))
    context, bot = admin_context(args=["purge"])
    await handlers.on_capture(command("/capture purge"), context)
    assert tg.sent_texts(bot) == [a.REPLY_STORAGE_DOWN]


async def test_capture_reports_storage_outage() -> None:
    store = InMemoryProfiles()
    handlers, _store, _archive = make_capture_handlers(store)
    store.fail_upserts = True
    context, bot = admin_context(args=["on"])
    await handlers.on_capture(command("/capture on"), context)
    assert tg.sent_texts(bot) == [a.REPLY_STORAGE_DOWN]


async def test_capture_off_during_outage_tombstones_for_convergence() -> None:
    store = InMemoryProfiles()
    handlers, _store, _archive = make_capture_handlers(store)
    service = handlers.capture_service
    assert service is not None
    # Capture is on and durable.
    on_context, _bot = admin_context(args=["on"])
    await handlers.on_capture(command("/capture on"), on_context)
    assert store.profiles[gscope()].capture_enabled

    # The disable write fails: the admin is told, and the scope is tombstoned
    # rather than left to resume off the stale `True` row.
    store.fail_upserts = True
    off_context, bot = admin_context(args=["off"])
    await handlers.on_capture(command("/capture off"), off_context)
    assert tg.sent_texts(bot) == [a.REPLY_STORAGE_DOWN]
    assert store.profiles[gscope()].capture_enabled  # stale True, revocation pending

    # Storage recovers; the maintenance tick converges the revocation.
    store.fail_upserts = False
    await service.retry_denied()
    assert not store.profiles[gscope()].capture_enabled


async def test_settings_reports_capture_state() -> None:
    store = InMemoryProfiles()
    handlers, _store, _archive = make_capture_handlers(store)
    context, bot = admin_context(args=["on"])
    await handlers.on_capture(command("/capture on"), context)

    context, bot = admin_context()
    await handlers.on_settings(command("/settings"), context)
    assert "message capture: on" in tg.sent_texts(bot)[0]


async def test_capture_requires_a_group_admin() -> None:
    handlers, _store, _archive = make_capture_handlers()
    context, bot = admin_context(status=ChatMemberStatus.MEMBER, args=["on"])
    await handlers.on_capture(command("/capture on"), context)
    assert tg.sent_texts(bot) == [a.REPLY_NOT_ADMIN]


async def test_subscribable_on_mints_a_share_link() -> None:
    store = InMemoryProfiles()
    handlers = make_handlers(store)
    context, bot = admin_context(args=["on"])
    await handlers.on_subscribable(command("/subscribable on"), context)
    code = store.profiles[gscope()].subscribe_code
    assert code
    assert f"start=sub_{code}" in tg.sent_texts(bot)[0]


async def test_subscribable_off_clears_the_code() -> None:
    store = InMemoryProfiles()
    handlers = make_handlers(store)
    await handlers.on_subscribable(command("/subscribable on"), admin_context(args=["on"])[0])
    assert store.profiles[gscope()].subscribe_code  # set

    context, bot = admin_context(args=["off"])
    await handlers.on_subscribable(command("/subscribable off"), context)
    assert store.profiles[gscope()].subscribe_code is None
    assert "OFF" in tg.sent_texts(bot)[0]


async def test_subscribable_requires_admin() -> None:
    handlers = make_handlers()
    context, bot = admin_context(status=ChatMemberStatus.MEMBER, args=["on"])
    await handlers.on_subscribable(command("/subscribable on"), context)
    assert tg.sent_texts(bot) == [a.REPLY_NOT_ADMIN]


async def test_subscribable_usage_on_bad_argument() -> None:
    handlers = make_handlers()
    context, bot = admin_context(args=["maybe"])
    await handlers.on_subscribable(command("/subscribable maybe"), context)
    assert tg.sent_texts(bot) == [a.REPLY_SUBSCRIBABLE_USAGE]


async def test_subscribable_reports_storage_outage() -> None:
    store = InMemoryProfiles()
    store.fail_upserts = True
    handlers = make_handlers(store)
    context, bot = admin_context(args=["on"])
    await handlers.on_subscribable(command("/subscribable on"), context)
    assert tg.sent_texts(bot) == [a.REPLY_STORAGE_DOWN]


def make_llm_handlers(store: InMemoryProfiles | None = None) -> a.AdminHandlers:
    handlers = make_handlers(store)
    # /llm's defaults + ceiling now live on the shared CommandService the
    # handler delegates to.
    handlers.commands.llm_defaults = LlmSettings()
    handlers.commands.llm_max_tokens_ceiling = 4096
    return handlers


async def test_llm_without_deployment_support_says_so() -> None:
    handlers = make_handlers()  # llm_defaults left unset
    context, bot = admin_context(args=["show"])
    await handlers.on_llm(command("/llm show"), context)
    assert tg.sent_texts(bot) == [cmd.REPLY_LLM_OFF_DEPLOY]


async def test_llm_usage_on_bad_subcommands() -> None:
    handlers = make_llm_handlers()
    for args in ([], ["set"], ["frobnicate"]):
        context, bot = admin_context(args=args)
        await handlers.on_llm(command("/llm"), context)
        assert tg.sent_texts(bot) == [cmd.REPLY_LLM_USAGE]


async def test_llm_show_reports_defaults_then_scope_settings() -> None:
    store = InMemoryProfiles()
    handlers = make_llm_handlers(store)
    context, bot = admin_context(args=["show"])
    await handlers.on_llm(command("/llm show"), context)
    assert "deployment defaults" in tg.sent_texts(bot)[0]
    assert "model:default" in tg.sent_texts(bot)[0]

    context, bot = admin_context(args=["set", "model:large", "lang:fr"])
    await handlers.on_llm(command("/llm set model:large lang:fr"), context)
    assert "model:large" in tg.sent_texts(bot)[0]
    assert store.profiles[gscope()].llm == LlmSettings(model="large", lang="fr")

    context, bot = admin_context(args=["show"])
    await handlers.on_llm(command("/llm show"), context)
    assert "set for this scope" in tg.sent_texts(bot)[0]


async def test_llm_in_a_topic_builds_on_the_inherited_group_settings() -> None:
    store = InMemoryProfiles()
    handlers = make_llm_handlers(store)
    context, _bot = admin_context(args=["set", "model:large", "lang:fr"])
    await handlers.on_llm(command("/llm set model:large lang:fr"), context)

    context, bot = admin_context(args=["show"])
    await handlers.on_llm(command_in_topic("/llm show", 7), context)
    assert "inherited from the parent scope" in tg.sent_texts(bot)[0]
    assert "model:large" in tg.sent_texts(bot)[0]

    # A partial edit in the topic keeps the inherited model/lang instead
    # of silently resetting them to deployment defaults.
    context, _bot = admin_context(args=["set", "temp:0.4"])
    await handlers.on_llm(command_in_topic("/llm set temp:0.4", 7), context)
    topic = store.profiles[gscope(tg.GROUP.id, 7)].llm
    assert topic == LlmSettings(model="large", lang="fr", temperature=0.4)


async def test_llm_topic_without_group_settings_shows_deployment_defaults() -> None:
    handlers = make_llm_handlers(InMemoryProfiles())
    context, bot = admin_context(args=["show"])
    await handlers.on_llm(command_in_topic("/llm show", 7), context)
    assert "deployment defaults" in tg.sent_texts(bot)[0]


async def test_llm_set_rejects_bad_values_verbatim() -> None:
    handlers = make_llm_handlers()
    context, bot = admin_context(args=["set", "temp:2"])
    await handlers.on_llm(command("/llm set temp:2"), context)
    assert "between 0 and 1" in tg.sent_texts(bot)[0]


async def test_llm_reset_returns_to_deployment_defaults() -> None:
    store = InMemoryProfiles()
    handlers = make_llm_handlers(store)
    context, _bot = admin_context(args=["set", "model:large"])
    await handlers.on_llm(command("/llm set model:large"), context)

    context, bot = admin_context(args=["reset"])
    await handlers.on_llm(command("/llm reset"), context)

    assert "deployment defaults" in tg.sent_texts(bot)[0]
    assert store.profiles[gscope()].llm is None


async def test_llm_reports_storage_outage() -> None:
    store = InMemoryProfiles(fail=True)
    handlers = make_llm_handlers(store)
    context, bot = admin_context(args=["show"])
    await handlers.on_llm(command("/llm show"), context)
    assert tg.sent_texts(bot) == [a.REPLY_STORAGE_DOWN]


async def test_llm_requires_a_group_admin() -> None:
    handlers = make_llm_handlers()
    context, bot = admin_context(status=ChatMemberStatus.MEMBER, args=["show"])
    await handlers.on_llm(command("/llm show"), context)
    assert tg.sent_texts(bot) == [a.REPLY_NOT_ADMIN]


def make_action_handlers(
    store: InMemoryProfiles | None = None,
) -> tuple[a.AdminHandlers, InMemoryActions]:
    handlers = make_handlers(store)
    actions = InMemoryActions()
    handlers.actions = actions
    handlers.clock = FakeClock()
    return handlers, actions


async def test_action_without_deployment_support_says_so() -> None:
    handlers = make_handlers()  # actions/clock left unset
    context, bot = admin_context(args=["list"])
    await handlers.on_action(command("/action list"), context)
    assert tg.sent_texts(bot) == [a.REPLY_ACTIONS_OFF_DEPLOY]


async def test_action_usage_on_bad_subcommands() -> None:
    handlers, _actions = make_action_handlers()
    for args in ([], ["add"], ["remove"], ["frobnicate"]):
        context, bot = admin_context(args=args)
        await handlers.on_action(command("/action"), context)
        assert tg.sent_texts(bot) == [a.REPLY_ACTION_USAGE]


async def test_action_add_stores_a_primed_spec_and_describes_it() -> None:
    handlers, actions = make_action_handlers()
    context, bot = admin_context(args=["add", "daily@06:00", "summarize", "model=large"])

    await handlers.on_action(command("/action add daily@06:00 summarize model=large"), context)

    (spec,) = actions.actions[gscope()]
    assert spec.trigger.schedule is not None
    assert spec.last_run == FakeClock().now()  # primed at creation
    reply = tg.sent_texts(bot)[0]
    assert reply.startswith("Scheduled for the group default:")
    assert "daily@06:00" in reply


async def test_action_add_relays_parse_errors_verbatim() -> None:
    handlers, _actions = make_action_handlers()
    context, bot = admin_context(args=["add", "hourly", "summarize"])
    await handlers.on_action(command("/action add hourly summarize"), context)
    assert "Unknown schedule" in tg.sent_texts(bot)[0]


async def test_action_add_enforces_the_per_scope_cap() -> None:
    handlers, actions = make_action_handlers()
    spec = parse_action("daily@06:00 summarize")
    actions.actions[gscope()] = tuple(
        replace(spec, action_id=f"a{i:03d}") for i in range(MAX_ACTIONS)
    )
    context, bot = admin_context(args=["add", "daily@07:00", "stats"])

    await handlers.on_action(command("/action add daily@07:00 stats"), context)

    assert "maximum" in tg.sent_texts(bot)[0]
    assert len(actions.actions[gscope()]) == MAX_ACTIONS


async def test_action_remove_and_list_round_trip() -> None:
    handlers, actions = make_action_handlers()
    context, _bot = admin_context(args=["add", "daily@06:00", "summarize"])
    await handlers.on_action(command("/action add daily@06:00 summarize"), context)
    (spec,) = actions.actions[gscope()]

    context, bot = admin_context(args=["list"])
    await handlers.on_action(command("/action list"), context)
    assert spec.action_id in tg.sent_texts(bot)[0]

    context, bot = admin_context(args=["remove", spec.action_id])
    await handlers.on_action(command(f"/action remove {spec.action_id}"), context)
    assert "removed" in tg.sent_texts(bot)[0]
    assert actions.actions[gscope()] == ()

    context, bot = admin_context(args=["remove", "zzzz"])
    await handlers.on_action(command("/action remove zzzz"), context)
    assert "No action" in tg.sent_texts(bot)[0]

    context, bot = admin_context(args=["list"])
    await handlers.on_action(command("/action list"), context)
    assert "No scheduled actions" in tg.sent_texts(bot)[0]


async def test_action_reports_storage_outage() -> None:
    handlers, actions = make_action_handlers()
    actions.fail = True
    context, bot = admin_context(args=["list"])
    await handlers.on_action(command("/action list"), context)
    assert tg.sent_texts(bot) == [a.REPLY_STORAGE_DOWN]


async def test_action_requires_a_group_admin() -> None:
    handlers, _actions = make_action_handlers()
    context, bot = admin_context(status=ChatMemberStatus.MEMBER, args=["list"])
    await handlers.on_action(command("/action list"), context)
    assert tg.sent_texts(bot) == [a.REPLY_NOT_ADMIN]


async def test_capture_purge_accepts_a_before_date() -> None:
    handlers, _store, archive = make_capture_handlers()
    archive.messages.extend(
        [
            CapturedMessage(
                scope=gscope(),
                message_id=1,
                posted_at=tg.NOW - timedelta(days=40),
                author="x",
            ),
            CapturedMessage(scope=gscope(), message_id=2, posted_at=tg.NOW, author="x"),
        ]
    )
    context, bot = admin_context(args=["purge", "before:2026-07-01"])

    await handlers.on_capture(command("/capture purge before:2026-07-01"), context)

    assert "1 message(s) deleted" in tg.sent_texts(bot)[0]
    assert len(archive.messages) == 1  # recent rows kept


async def test_capture_purge_rejects_a_bad_before_date() -> None:
    handlers, _store, _archive = make_capture_handlers()
    for bad in ["before:soon", "before:2026-13-01", "until:2026-01-01"]:
        context, bot = admin_context(args=["purge", bad])
        await handlers.on_capture(command(f"/capture purge {bad}"), context)
        assert tg.sent_texts(bot) == [a.REPLY_CAPTURE_USAGE]
