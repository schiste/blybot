"""AdminHandlers tests: live admin gate, self-service commands (spec v2)."""

from __future__ import annotations

from types import SimpleNamespace
from typing import TYPE_CHECKING

from telegram import Update
from telegram.constants import ChatMemberStatus
from telegram.error import TelegramError
from telegram.ext import ContextTypes

from blybot.adapters.telegram import admin as a
from blybot.domain.models import ConsentMode, EventKind, EventType
from blybot.observability import Counters
from blybot.services.binding import TokenBinding
from blybot.services.directory import ChannelDirectory
from blybot.services.policy import GroupPolicy
from blybot.services.rules import MAX_RULES
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


async def test_events_on_requires_a_repo_at_this_scope() -> None:
    store = InMemoryProfiles()
    handlers = make_handlers(store)
    context, bot = admin_context(args=["on"])
    await handlers.on_events(command("/events on"), context)  # no repo bound
    assert tg.sent_texts(bot) == [a.REPLY_EVENTS_NEED_REPO]
    assert (tg.GROUP.id, 0) not in store.profiles or not store.profiles[
        tg.GROUP.id, 0
    ].events_enabled


async def test_events_on_uses_default_kinds() -> None:
    store = InMemoryProfiles()
    handlers = make_handlers(store)
    await handlers.directory.set_repo(tg.GROUP.id, 0, "org/repo")
    context, bot = admin_context(args=["on"])
    await handlers.on_events(command("/events on"), context)
    profile = store.profiles[tg.GROUP.id, 0]
    assert profile.events_enabled
    assert profile.event_kinds == a.DEFAULT_EVENT_KINDS
    assert tg.sent_texts(bot) == [a.REPLY_EVENTS_SET.format(state="prs, releases")]


async def test_events_accepts_explicit_kinds_and_off() -> None:
    store = InMemoryProfiles()
    handlers = make_handlers(store)
    await handlers.directory.set_repo(tg.GROUP.id, 0, "org/repo")
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


async def test_commands_configure_the_topic_they_are_run_in() -> None:
    store = InMemoryProfiles()
    handlers = make_handlers(store)
    context, bot = admin_context(args=["WikiProject", "Foo"])
    await handlers.on_setpage(command_in_topic("/setpage WikiProject Foo", 42), context)

    assert (tg.GROUP.id, 42) in store.profiles  # the topic, not the group default
    assert store.profiles[tg.GROUP.id, 42].log_page == "WikiProject Foo/Telegram logs"
    (sent, thread) = tg.sent_calls(bot)[0]
    assert thread == 42  # confirmation routed back into the topic
    assert "this topic" in sent


async def test_setconsent_says_group_wide_even_from_a_topic() -> None:
    store = InMemoryProfiles()
    handlers = make_handlers(store)
    context, bot = admin_context(args=["author_only"])
    await handlers.on_setconsent(command_in_topic("/setconsent author_only", 42), context)
    assert store.profiles[tg.GROUP.id, 0].consent_mode is ConsentMode.AUTHOR_ONLY  # thread 0
    assert (tg.GROUP.id, 42) not in store.profiles
    assert "group-wide" in tg.sent_texts(bot)[0]


async def test_events_storage_failure_after_repo_check_is_reported() -> None:
    store = InMemoryProfiles()
    handlers = make_handlers(store)
    await handlers.directory.set_repo(tg.GROUP.id, 0, "org/repo")
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
    (rule,) = store.profiles[tg.GROUP.id, 0].rules
    assert rule.trigger is EventType.PR_MERGED
    assert rule.filter.base == "main"
    (sent,) = tg.sent_texts(bot)
    assert sent.startswith(f"Rule added for the group default: [{rule.rule_id}] pr.merged")


async def test_rules_lists_every_rule_with_ids() -> None:
    store = InMemoryProfiles()
    handlers = make_handlers(store)
    for spec in (["add", "pr.merged"], ["add", "issue.opened", "label:bug"]):
        context, _ = admin_context(args=spec)
        await handlers.on_rule(command("/rule"), context)
    context, bot = admin_context()
    await handlers.on_rules(command("/rules"), context)
    listing = tg.sent_texts(bot)[-1]
    assert listing.startswith("Rules for the group default:")
    assert "pr.merged" in listing
    assert "issue.opened label:bug → live" in listing


async def test_rules_empty_prompts_to_add_one() -> None:
    handlers = make_handlers()
    context, bot = admin_context()
    await handlers.on_rules(command("/rules"), context)
    assert tg.sent_texts(bot) == [a.REPLY_RULES_NONE.format(scope="the group default")]


async def test_rule_remove_by_id_and_unknown() -> None:
    store = InMemoryProfiles()
    handlers = make_handlers(store)
    context, _ = admin_context(args=["add", "release"])
    await handlers.on_rule(command("/rule add release"), context)
    (rule,) = store.profiles[tg.GROUP.id, 0].rules

    context, bot = admin_context(args=["remove", "nope"])
    await handlers.on_rule(command("/rule remove nope"), context)
    assert tg.sent_texts(bot) == [a.REPLY_RULE_UNKNOWN.format(id="nope", scope="the group default")]

    context, bot = admin_context(args=["remove", rule.rule_id])
    await handlers.on_rule(command("/rule remove id"), context)
    assert store.profiles[tg.GROUP.id, 0].rules == ()
    assert tg.sent_texts(bot) == [
        a.REPLY_RULE_REMOVED.format(id=rule.rule_id, scope="the group default")
    ]


async def test_rule_clear_reports_count() -> None:
    store = InMemoryProfiles()
    handlers = make_handlers(store)
    for spec in (["add", "pr.merged"], ["add", "release"]):
        context, _ = admin_context(args=spec)
        await handlers.on_rule(command("/rule"), context)
    context, bot = admin_context(args=["clear"])
    await handlers.on_rule(command("/rule clear"), context)
    assert store.profiles[tg.GROUP.id, 0].rules == ()
    assert tg.sent_texts(bot) == [a.REPLY_RULES_CLEARED.format(count=2, scope="the group default")]


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
        assert tg.sent_texts(bot) == [a.REPLY_RULE_USAGE]


async def test_rule_cap_is_enforced() -> None:
    store = InMemoryProfiles()
    handlers = make_handlers(store)
    for _ in range(MAX_RULES):
        context, _ = admin_context(args=["add", "pr.merged"])
        await handlers.on_rule(command("/rule add pr.merged"), context)
    context, bot = admin_context(args=["add", "release"])
    await handlers.on_rule(command("/rule add release"), context)
    assert len(store.profiles[tg.GROUP.id, 0].rules) == MAX_RULES  # not exceeded
    assert tg.sent_texts(bot) == [
        a.REPLY_RULES_FULL.format(max=MAX_RULES, scope="the group default")
    ]


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
    assert store.profiles[tg.GROUP.id, 42].rules  # the topic, not the group default
    (_sent, thread) = tg.sent_calls(bot)[0]
    assert thread == 42
    assert "this topic" in tg.sent_texts(bot)[0]


def test_thread_of_ignores_non_topic_and_missing_messages() -> None:
    assert a._thread_of(Update(update_id=1)) == 0  # no message
    plain = tg.command_update(tg.message(text="/x"))  # not a topic message
    assert a._thread_of(plain) == 0
    topical = tg.command_update(tg.message(text="/x", thread_id=42))
    assert a._thread_of(topical) == 42
