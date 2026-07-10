"""Group /log flow handler tests (spec R1-R3, N4, consent policy)."""

from __future__ import annotations

import asyncio
from datetime import timedelta
from types import SimpleNamespace

from telegram import Chat, Message, Update, User
from telegram.constants import ChatType
from telegram.error import TelegramError

from blybot.adapters.telegram import handlers as h
from blybot.domain.models import ConsentMode, TimestampGranularity
from blybot.observability import Counters
from blybot.services.directory import ChannelDirectory
from blybot.services.policy import GroupPolicy, SlidingWindowLimiter
from blybot.services.publish import LogPublicationService
from tests import tg
from tests.fakes import (
    FailingPublisher,
    FakeClock,
    FakePublisher,
    InMemoryProfiles,
    PassthroughSanitizer,
    SequentialPseudonyms,
)

LOG_PAGE = "Meta:Community/Log"
LOG_PAGE_URL = "https://meta.wikimedia.org/wiki/Meta:Community/Log"


def page_url_for(title: str) -> str:
    return f"https://meta.wikimedia.org/wiki/{title.replace(' ', '_')}"


def make_handlers(
    publisher: FakePublisher | FailingPublisher | None = None,
    consent_mode: ConsentMode = ConsentMode.IMMEDIATE,
    allowed: set[int] | None = None,
    limit: int = 100,
    cleanup_delay_seconds: float = 0,
    reply_cleanup_delay_seconds: float = 0,
    newcomer_welcome_enabled: bool = True,
) -> tuple[h.GroupHandlers, FakePublisher | FailingPublisher, GroupPolicy]:
    publisher = publisher if publisher is not None else FakePublisher()
    policy = GroupPolicy(allowed=allowed if allowed is not None else set())
    handlers = h.GroupHandlers(
        log_service=LogPublicationService(
            publisher=publisher,
            sanitizer=PassthroughSanitizer(),
            pseudonyms=SequentialPseudonyms(),
            clock=FakeClock(),
            target_page=LOG_PAGE,
            edit_summary="Log entry via Blybot",
            timestamp_granularity=TimestampGranularity.NONE,
        ),
        groups=policy,
        limiter=SlidingWindowLimiter(clock=FakeClock(), limit=limit, window=timedelta(minutes=1)),
        directory=ChannelDirectory(
            store=InMemoryProfiles(),
            default_log_page=LOG_PAGE,
            default_consent=consent_mode,
            default_repo="schiste/blybot",
            page_prefix="Telegram logs/",
        ),
        page_url_for=page_url_for,
        counters=Counters(),
        group_greeting_text="Hello, I am Blybot.",
        maintainer="Test Maintainer",
        cleanup_delay_seconds=cleanup_delay_seconds,
        reply_cleanup_delay_seconds=reply_cleanup_delay_seconds,
        newcomer_welcome_enabled=newcomer_welcome_enabled,
    )
    return handlers, publisher, policy


def log_command(target: Message | None, sender: User = tg.BOB) -> Update:
    return tg.command_update(tg.message(text="/log", from_user=sender, reply_to=target))


async def test_log_publishes_target_text_and_confirms() -> None:
    handlers, publisher, _ = make_handlers()
    context, bot = tg.make_context()
    target = tg.message(text="we decided X", from_user=tg.ALICE)

    await handlers.on_log(log_command(target), context)

    assert isinstance(publisher, FakePublisher)
    (page, _, text, _) = publisher.started[0]
    assert page == LOG_PAGE
    assert "[sanitized]we decided X" in text
    # Confirmation links straight to the created section (Anon-1: no
    # timestamp at NONE granularity, so the heading is the pseudonym).
    assert tg.sent_texts(bot) == [h.REPLY_PUBLISHED.format(url=f"{LOG_PAGE_URL}#Anon-1")]


async def test_log_without_reply_explains_usage() -> None:
    handlers, publisher, _ = make_handlers()
    context, bot = tg.make_context()
    await handlers.on_log(log_command(None), context)
    assert isinstance(publisher, FakePublisher)
    assert publisher.wrote_nothing
    assert tg.sent_texts(bot) == [h.REPLY_USAGE]


async def test_log_on_media_only_message_declines(  # R2: media-only
) -> None:
    handlers, publisher, _ = make_handlers()
    context, bot = tg.make_context()
    target = tg.message(text=None, from_user=tg.ALICE)
    await handlers.on_log(log_command(target), context)
    assert isinstance(publisher, FakePublisher)
    assert publisher.wrote_nothing
    assert tg.sent_texts(bot) == [h.REPLY_MEDIA_DECLINED]


async def test_log_in_unlisted_group_is_ignored_silently() -> None:
    handlers, publisher, _ = make_handlers(allowed={-42})
    context, bot = tg.make_context()
    await handlers.on_log(log_command(tg.message(text="x")), context)
    assert isinstance(publisher, FakePublisher)
    assert publisher.wrote_nothing
    assert tg.sent_texts(bot) == []


async def test_log_in_private_chat_explains_the_gesture() -> None:
    """Silent ignore reads as breakage (proven in the field): explain instead."""
    handlers, publisher, _ = make_handlers()
    context, bot = tg.make_context()
    command = tg.message(chat=tg.PRIVATE, text="/log")
    await handlers.on_log(tg.command_update(command), context)
    assert isinstance(publisher, FakePublisher)
    assert publisher.wrote_nothing
    assert tg.sent_texts(bot) == [h.REPLY_LOG_IS_GROUP_ONLY]


async def test_log_in_a_channel_is_ignored_silently() -> None:
    handlers, publisher, _ = make_handlers()
    context, bot = tg.make_context()
    channel = Chat(id=-100777, type=ChatType.CHANNEL)
    command = tg.message(chat=channel, text="/log", from_user=None)
    await handlers.on_log(tg.command_update(command), context)
    assert isinstance(publisher, FakePublisher)
    assert publisher.wrote_nothing
    assert tg.sent_texts(bot) == []


async def test_log_update_without_a_message_is_ignored() -> None:
    handlers, publisher, _ = make_handlers()
    context, bot = tg.make_context()
    await handlers.on_log(Update(update_id=5), context)
    assert isinstance(publisher, FakePublisher)
    assert publisher.wrote_nothing
    assert tg.sent_texts(bot) == []


async def test_author_only_mode_blocks_logging_others() -> None:
    handlers, publisher, _ = make_handlers(consent_mode=ConsentMode.AUTHOR_ONLY)
    context, bot = tg.make_context()
    target = tg.message(text="Alice's words", from_user=tg.ALICE)
    await handlers.on_log(log_command(target, sender=tg.BOB), context)
    assert isinstance(publisher, FakePublisher)
    assert publisher.wrote_nothing
    assert tg.sent_texts(bot) == [h.REPLY_AUTHOR_ONLY]


async def test_author_only_mode_allows_logging_your_own_message() -> None:
    handlers, publisher, _ = make_handlers(consent_mode=ConsentMode.AUTHOR_ONLY)
    context, _ = tg.make_context()
    target = tg.message(text="my own words", from_user=tg.ALICE)
    await handlers.on_log(log_command(target, sender=tg.ALICE), context)
    assert isinstance(publisher, FakePublisher)
    assert len(publisher.started) == 1


async def test_flooding_is_throttled(  # N4
) -> None:
    handlers, publisher, _ = make_handlers(limit=2)
    context, bot = tg.make_context()
    for _ in range(3):
        await handlers.on_log(log_command(tg.message(text="x", from_user=tg.ALICE)), context)
    assert isinstance(publisher, FakePublisher)
    assert len(publisher.started) == 2
    assert tg.sent_texts(bot)[-1] == h.REPLY_THROTTLED


async def test_wiki_failure_reports_neutrally() -> None:
    handlers, _, _ = make_handlers(publisher=FailingPublisher())
    context, bot = tg.make_context()
    await handlers.on_log(log_command(tg.message(text="x")), context)
    assert tg.sent_texts(bot) == [h.REPLY_WIKI_ERROR]


async def test_greets_once_on_joining_a_group(  # R3
) -> None:
    handlers, _, _ = make_handlers()
    context, bot = tg.make_context()
    await handlers.on_my_chat_member(
        tg.membership_update(tg.GROUP, user=tg.ALICE, joined=True, mine=True), context
    )
    assert tg.sent_texts(bot) == ["Hello, I am Blybot."]


async def test_does_not_greet_when_leaving_or_in_unlisted_groups() -> None:
    handlers, _, _ = make_handlers(allowed={-42})
    context, bot = tg.make_context()
    await handlers.on_my_chat_member(
        tg.membership_update(tg.GROUP, user=tg.ALICE, joined=True, mine=True), context
    )
    left = tg.membership_update(tg.GROUP, user=tg.ALICE, joined=False, mine=True)
    handlers.groups.allowed.clear()
    await handlers.on_my_chat_member(left, context)
    assert tg.sent_texts(bot) == []


async def test_supergroup_migration_updates_the_allowlist(  # spec 8
) -> None:
    handlers, _, policy = make_handlers(allowed={tg.GROUP.id})
    context, _ = tg.make_context()
    service_message = tg.message(text=None, migrate_to_chat_id=-100999)
    await handlers.on_migration(tg.command_update(service_message), context)
    assert policy.is_allowed(-100999)
    assert not policy.is_allowed(tg.GROUP.id)


async def test_membership_updates_outside_groups_are_ignored() -> None:
    handlers, _, _ = make_handlers()
    context, bot = tg.make_context()
    private_change = tg.membership_update(tg.PRIVATE, user=tg.ALICE, joined=True, mine=True)
    await handlers.on_my_chat_member(private_change, context)
    assert tg.sent_texts(bot) == []


async def test_regular_messages_are_not_migrations() -> None:
    handlers, _, policy = make_handlers(allowed={tg.GROUP.id})
    context, _ = tg.make_context()
    await handlers.on_migration(tg.command_update(tg.message(text="hello")), context)
    assert policy.allowed == {tg.GROUP.id}  # untouched


async def test_newcomer_handler_ignores_updates_without_membership_change() -> None:
    handlers, _, _ = make_handlers()
    context, bot = tg.make_context()
    await handlers.on_newcomer(tg.command_update(tg.message(text="hi")), context)
    assert tg.sent_texts(bot) == []


async def test_group_help_explains_the_log_gesture() -> None:
    handlers, _, _ = make_handlers()
    context, bot = tg.make_context()
    await handlers.on_help(tg.command_update(tg.message(text="/help")), context)
    (sent,) = tg.sent_texts(bot)
    assert "/log" in sent
    assert LOG_PAGE_URL in sent
    assert "maintained by Test Maintainer" in sent


async def test_group_help_stays_silent_in_unlisted_groups_and_dms() -> None:
    handlers, _, _ = make_handlers(allowed={-42})
    context, bot = tg.make_context()
    await handlers.on_help(tg.command_update(tg.message(text="/help")), context)
    await handlers.on_help(tg.command_update(tg.message(chat=tg.PRIVATE, text="/help")), context)
    assert tg.sent_texts(bot) == []


async def test_log_command_message_is_deleted_after_handling() -> None:
    """The command's deletion hides who requested the publication."""
    handlers, _, _ = make_handlers()
    context, bot = tg.make_context()
    bot.send_message.return_value = SimpleNamespace(message_id=99)
    await handlers.on_log(log_command(tg.message(text="x")), context)

    deleted = [call.kwargs["message_id"] for call in bot.delete_message.await_args_list]
    assert deleted == [10, 99]  # the command, then the bot's own confirmation


async def test_command_cleanup_without_delete_right_is_silent() -> None:
    """Bots need the 'Delete messages' admin right to remove others' messages."""
    handlers, publisher, _ = make_handlers()
    context, bot = tg.make_context()
    bot.delete_message.side_effect = TelegramError("not enough rights")
    await handlers.on_log(log_command(tg.message(text="x")), context)

    assert isinstance(publisher, FakePublisher)
    assert len(publisher.started) == 1  # publication is unaffected


async def test_command_cleanup_runs_as_a_background_task_when_delayed() -> None:
    handlers, _, _ = make_handlers(cleanup_delay_seconds=3600, reply_cleanup_delay_seconds=3600)
    context, bot = tg.make_context()
    await handlers.on_log(log_command(tg.message(text="x")), context)

    assert bot.delete_message.await_count == 0  # both deletions still pending
    tasks = list(handlers._cleanup_tasks)
    assert len(tasks) == 2  # the command and the confirmation
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
    assert not handlers._cleanup_tasks  # done-callback pruned the registry


async def test_cleanup_can_be_disabled_entirely() -> None:
    handlers, _, _ = make_handlers(cleanup_delay_seconds=-1, reply_cleanup_delay_seconds=-1)
    context, bot = tg.make_context()
    await handlers.on_log(log_command(tg.message(text="x")), context)
    bot.delete_message.assert_not_awaited()


async def test_consent_policy_is_resolved_per_group() -> None:
    """A group's stored consent policy overrides the deployment default."""
    handlers, publisher, _ = make_handlers(consent_mode=ConsentMode.IMMEDIATE)
    await handlers.directory.set_consent(tg.GROUP.id, ConsentMode.AUTHOR_ONLY)
    context, bot = tg.make_context()
    target = tg.message(text="Alice's words", from_user=tg.ALICE)
    await handlers.on_log(log_command(target, sender=tg.BOB), context)

    assert isinstance(publisher, FakePublisher)
    assert publisher.wrote_nothing
    assert h.REPLY_AUTHOR_ONLY in tg.sent_texts(bot)


async def test_log_publishes_to_the_group_configured_page() -> None:
    handlers, publisher, _ = make_handlers()
    await handlers.directory.set_log_page(tg.GROUP.id, "Telegram logs/Ours")
    context, bot = tg.make_context()
    await handlers.on_log(log_command(tg.message(text="x")), context)

    assert isinstance(publisher, FakePublisher)
    assert publisher.started[0][0] == "Telegram logs/Ours"
    assert "Telegram_logs/Ours#" in tg.sent_texts(bot)[0]
