"""Group /log flow handler tests (spec R1-R3, N4, consent policy)."""

from __future__ import annotations

from datetime import timedelta

from telegram import Message, Update, User

from blybot.adapters.telegram import handlers as h
from blybot.domain.models import ConsentMode, TimestampGranularity
from blybot.domain.ports import WikiWriteError
from blybot.observability import Counters
from blybot.services.policy import GroupPolicy, SlidingWindowLimiter
from blybot.services.publish import LogPublicationService
from tests import tg
from tests.fakes import FakeClock, FakePublisher, PassthroughSanitizer

LOG_PAGE = "Meta:Community/Log"


class FailingPublisher:
    async def append(self, page: str, text: str, summary: str) -> None:  # noqa: ARG002
        raise WikiWriteError


def make_handlers(
    publisher: FakePublisher | FailingPublisher | None = None,
    consent_mode: ConsentMode = ConsentMode.IMMEDIATE,
    allowed: set[int] | None = None,
    limit: int = 100,
) -> tuple[h.GroupHandlers, FakePublisher | FailingPublisher, GroupPolicy]:
    publisher = publisher if publisher is not None else FakePublisher()
    policy = GroupPolicy(allowed=allowed if allowed is not None else set())
    handlers = h.GroupHandlers(
        log_service=LogPublicationService(
            publisher=publisher,
            sanitizer=PassthroughSanitizer(),
            clock=FakeClock(),
            target_page=LOG_PAGE,
            edit_summary="Log entry via Blybot",
            timestamp_granularity=TimestampGranularity.NONE,
        ),
        groups=policy,
        limiter=SlidingWindowLimiter(clock=FakeClock(), limit=limit, window=timedelta(minutes=1)),
        consent_mode=consent_mode,
        counters=Counters(),
        group_greeting_text="Hello, I am Blybot.",
        log_page=LOG_PAGE,
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
    (page, text, _) = publisher.appends[0]
    assert page == LOG_PAGE
    assert "[sanitized]we decided X" in text
    assert tg.sent_texts(bot) == [h.REPLY_PUBLISHED.format(page=LOG_PAGE)]


async def test_log_without_reply_explains_usage() -> None:
    handlers, publisher, _ = make_handlers()
    context, bot = tg.make_context()
    await handlers.on_log(log_command(None), context)
    assert isinstance(publisher, FakePublisher)
    assert publisher.appends == []
    assert tg.sent_texts(bot) == [h.REPLY_USAGE]


async def test_log_on_media_only_message_declines(  # R2: media-only
) -> None:
    handlers, publisher, _ = make_handlers()
    context, bot = tg.make_context()
    target = tg.message(text=None, from_user=tg.ALICE)
    await handlers.on_log(log_command(target), context)
    assert isinstance(publisher, FakePublisher)
    assert publisher.appends == []
    assert tg.sent_texts(bot) == [h.REPLY_MEDIA_DECLINED]


async def test_log_in_unlisted_group_is_ignored_silently() -> None:
    handlers, publisher, _ = make_handlers(allowed={-42})
    context, bot = tg.make_context()
    await handlers.on_log(log_command(tg.message(text="x")), context)
    assert isinstance(publisher, FakePublisher)
    assert publisher.appends == []
    assert tg.sent_texts(bot) == []


async def test_log_outside_a_group_is_ignored() -> None:
    handlers, publisher, _ = make_handlers()
    context, bot = tg.make_context()
    command = tg.message(chat=tg.PRIVATE, text="/log")
    await handlers.on_log(tg.command_update(command), context)
    assert isinstance(publisher, FakePublisher)
    assert publisher.appends == []
    assert tg.sent_texts(bot) == []


async def test_author_only_mode_blocks_logging_others() -> None:
    handlers, publisher, _ = make_handlers(consent_mode=ConsentMode.AUTHOR_ONLY)
    context, bot = tg.make_context()
    target = tg.message(text="Alice's words", from_user=tg.ALICE)
    await handlers.on_log(log_command(target, sender=tg.BOB), context)
    assert isinstance(publisher, FakePublisher)
    assert publisher.appends == []
    assert tg.sent_texts(bot) == [h.REPLY_AUTHOR_ONLY]


async def test_author_only_mode_allows_logging_your_own_message() -> None:
    handlers, publisher, _ = make_handlers(consent_mode=ConsentMode.AUTHOR_ONLY)
    context, _ = tg.make_context()
    target = tg.message(text="my own words", from_user=tg.ALICE)
    await handlers.on_log(log_command(target, sender=tg.ALICE), context)
    assert isinstance(publisher, FakePublisher)
    assert len(publisher.appends) == 1


async def test_flooding_is_throttled(  # N4
) -> None:
    handlers, publisher, _ = make_handlers(limit=2)
    context, bot = tg.make_context()
    for _ in range(3):
        await handlers.on_log(log_command(tg.message(text="x", from_user=tg.ALICE)), context)
    assert isinstance(publisher, FakePublisher)
    assert len(publisher.appends) == 2
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
