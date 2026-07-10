"""DM session and newcomer welcome handler tests (spec R4, R5)."""

from __future__ import annotations

from datetime import timedelta

from telegram import Update, User

from blybot.adapters.telegram import handlers as h
from blybot.observability import Counters
from tests import tg
from tests.fakes import FailingPublisher, FakeClock, FakePublisher
from tests.test_group_handlers import make_handlers as make_group_handlers
from tests.test_transcribe import make_service

TTL = timedelta(minutes=45)


def make_handlers(clock: FakeClock | None = None) -> tuple[h.PrivateHandlers, FakePublisher]:
    publisher = FakePublisher()
    transcription = make_service(publisher, clock or FakeClock())
    handlers = h.PrivateHandlers(
        transcription=transcription,
        sessions=transcription.sessions,
        counters=Counters(),
        welcome_text="Welcome to Blybot.",
        dm_page_url="https://meta.wikimedia.org/wiki/Meta_talk:Community/Discussions",
        maintainer="Test Maintainer",
    )
    return handlers, publisher


def dm(text: str | None) -> Update:
    return tg.command_update(tg.message(chat=tg.PRIVATE, text=text, from_user=tg.ALICE))


async def test_start_delivers_the_welcome_and_nothing_else() -> None:
    """/start is the doorway (R5): welcome copy only — no session side effects."""
    handlers, _ = make_handlers()
    context, bot = tg.make_context()
    await handlers.on_start(dm("/start welcome"), context)

    assert tg.sent_texts(bot) == ["Welcome to Blybot."]
    assert handlers.sessions.peek(tg.PRIVATE.id) is None  # no identity minted


async def test_flush_forces_a_fresh_identity_and_announces_it() -> None:
    handlers, _ = make_handlers()
    context, bot = tg.make_context()
    await handlers.on_dm(dm("hello"), context)  # session Anon-1 opens lazily
    await handlers.on_flush(dm("/flush"), context)

    session = handlers.sessions.peek(tg.PRIVATE.id)
    assert session is not None
    assert session.pseudonym.value == "Anon-2"
    assert "Anon-2" in tg.sent_texts(bot)[-1]
    assert h.REPLY_FLUSHED.strip() in tg.sent_texts(bot)[-1]


async def test_whoami_discloses_without_rotating() -> None:
    handlers, _ = make_handlers()
    context, bot = tg.make_context()
    await handlers.on_dm(dm("hello"), context)
    await handlers.on_whoami(dm("/whoami"), context)

    assert "Anon-1" in tg.sent_texts(bot)[-1]
    session = handlers.sessions.peek(tg.PRIVATE.id)
    assert session is not None
    assert session.pseudonym.value == "Anon-1"  # unchanged


async def test_whoami_without_a_session_explains_lazy_minting() -> None:
    handlers, _ = make_handlers()
    context, bot = tg.make_context()
    await handlers.on_whoami(dm("/whoami"), context)
    assert tg.sent_texts(bot) == [h.REPLY_NO_SESSION]


async def test_private_help_lists_the_commands() -> None:
    handlers, _ = make_handlers()
    context, bot = tg.make_context()
    await handlers.on_help(dm("/help"), context)
    (sent,) = tg.sent_texts(bot)
    for command in ("/whoami", "/flush", "/privacy", "/log"):
        assert command in sent
    assert "https://meta.wikimedia.org/wiki/Meta_talk:Community/Discussions" in sent
    assert "maintained by Test Maintainer" in sent


async def test_privacy_statement_covers_the_guarantees() -> None:
    handlers, _ = make_handlers()
    context, bot = tg.make_context()
    await handlers.on_privacy(dm("/privacy"), context)
    (sent,) = tg.sent_texts(bot)
    assert "pseudonym" in sent
    assert "permanently" in sent


async def test_private_commands_outside_private_chats_are_ignored() -> None:
    handlers, _ = make_handlers()
    context, bot = tg.make_context()
    group_update = tg.command_update(tg.message(chat=tg.GROUP, text="/x"))
    for handler in (
        handlers.on_start,
        handlers.on_flush,
        handlers.on_whoami,
        handlers.on_help,
        handlers.on_privacy,
    ):
        await handler(group_update, context)
    assert tg.sent_texts(bot) == []


async def test_dm_is_transcribed_under_the_session_pseudonym() -> None:
    handlers, publisher = make_handlers()
    context, _ = tg.make_context()
    await handlers.on_dm(dm("hello there"), context)

    (page, heading, text, _) = publisher.started[0]
    assert page == "Meta talk:Community/Discussions"
    assert heading == "Anon-1"
    assert text == ": [sanitized]hello there"


async def test_first_dm_announces_the_identity_then_stays_quiet() -> None:
    handlers, _ = make_handlers()
    context, bot = tg.make_context()
    await handlers.on_dm(dm("first"), context)
    await handlers.on_dm(dm("second"), context)

    (announcement,) = tg.sent_texts(bot)
    assert "Anon-1" in announcement


async def test_session_rollover_mid_conversation_is_announced() -> None:
    clock = FakeClock()
    handlers, _ = make_handlers(clock)
    context, bot = tg.make_context()
    await handlers.on_dm(dm("before"), context)
    clock.advance(TTL)
    await handlers.on_dm(dm("after"), context)

    texts = tg.sent_texts(bot)
    assert len(texts) == 2
    assert "Anon-2" in texts[1]


async def test_group_messages_never_reach_transcription() -> None:
    handlers, publisher = make_handlers()
    context, _ = tg.make_context()
    group_msg = tg.command_update(tg.message(chat=tg.GROUP, text="group chatter"))
    await handlers.on_dm(group_msg, context)
    assert publisher.wrote_nothing


async def test_newcomer_gets_a_deep_link_button_not_a_dm() -> None:
    group_handlers, _, _ = make_group_handlers()
    context, bot = tg.make_context()
    join = tg.membership_update(tg.GROUP, user=tg.ALICE, joined=True, mine=False)
    await group_handlers.on_newcomer(join, context)

    call = bot.send_message.await_args
    assert call is not None
    assert call.kwargs["chat_id"] == tg.GROUP.id  # posted in the group, never a DM
    assert call.kwargs["text"] == h.NEWCOMER_PROMPT
    button = call.kwargs["reply_markup"].inline_keyboard[0][0]
    assert button.url == "https://t.me/blybot_bot?start=welcome"


async def test_joining_bots_are_not_welcomed() -> None:
    group_handlers, _, _ = make_group_handlers()
    context, bot = tg.make_context()
    robot = User(id=99, first_name="OtherBot", is_bot=True)
    join = tg.membership_update(tg.GROUP, user=robot, joined=True, mine=False)
    await group_handlers.on_newcomer(join, context)
    assert tg.sent_texts(bot) == []


async def test_newcomers_in_unlisted_groups_are_ignored() -> None:
    group_handlers, _, _ = make_group_handlers(allowed={-42})
    context, bot = tg.make_context()
    join = tg.membership_update(tg.GROUP, user=tg.ALICE, joined=True, mine=False)
    await group_handlers.on_newcomer(join, context)
    assert tg.sent_texts(bot) == []


async def test_dm_without_text_is_ignored() -> None:
    handlers, publisher = make_handlers()
    context, bot = tg.make_context()
    await handlers.on_dm(dm(None), context)
    assert publisher.wrote_nothing
    assert tg.sent_texts(bot) == []


async def test_dm_wiki_failure_reports_neutrally_and_skips_the_announcement() -> None:
    transcription = make_service(FailingPublisher(), FakeClock())
    handlers = h.PrivateHandlers(
        transcription=transcription,
        sessions=transcription.sessions,
        counters=Counters(),
        welcome_text="Welcome.",
        dm_page_url="https://example.org/wiki/D",
        maintainer="",
    )
    context, bot = tg.make_context()
    await handlers.on_dm(dm("doomed"), context)
    assert tg.sent_texts(bot) == [h.REPLY_WIKI_ERROR]


def test_help_footer_omits_the_maintainer_line_when_unset() -> None:
    footer = h._help_footer("https://example.org/wiki/P", "")
    assert "lands at https://example.org/wiki/P" in footer
    assert "maintained" not in footer
