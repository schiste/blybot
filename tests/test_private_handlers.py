"""DM session and newcomer welcome handler tests (spec R4, R5)."""

from __future__ import annotations

from datetime import timedelta

from telegram import Update, User

from blybot.adapters.telegram import handlers as h
from blybot.observability import Counters
from blybot.services.sessions import SessionRegistry
from blybot.services.transcribe import DmTranscriptionService
from tests import tg
from tests.fakes import FakeClock, FakePublisher, PassthroughSanitizer, SequentialPseudonyms
from tests.test_group_handlers import make_handlers as make_group_handlers

TTL = timedelta(minutes=45)


def make_handlers(clock: FakeClock | None = None) -> tuple[h.PrivateHandlers, FakePublisher]:
    clock = clock or FakeClock()
    publisher = FakePublisher()
    sessions = SessionRegistry(pseudonyms=SequentialPseudonyms(), clock=clock, ttl=TTL)
    handlers = h.PrivateHandlers(
        transcription=DmTranscriptionService(
            publisher=publisher,
            sanitizer=PassthroughSanitizer(),
            sessions=sessions,
            target_page="Meta:Community/Discussions",
            edit_summary="Log entry via Blybot",
            debounce_seconds=0,
        ),
        sessions=sessions,
        counters=Counters(),
        welcome_text="Welcome to Blybot.",
    )
    return handlers, publisher


def dm(text: str | None) -> Update:
    return tg.command_update(tg.message(chat=tg.PRIVATE, text=text, from_user=tg.ALICE))


async def test_start_welcomes_and_opens_a_pseudonymous_session() -> None:
    handlers, _ = make_handlers()
    context, bot = tg.make_context()
    await handlers.on_start(dm("/start welcome"), context)

    (sent,) = tg.sent_texts(bot)
    assert sent.startswith("Welcome to Blybot.")
    assert "Anon-1" in sent
    assert "Meta:Community/Discussions#Anon-1" in sent


async def test_start_always_forces_a_fresh_identity() -> None:
    handlers, _ = make_handlers()
    context, _ = tg.make_context()
    await handlers.on_start(dm("/start"), context)
    await handlers.on_start(dm("/start"), context)
    session = handlers.sessions.peek(tg.PRIVATE.id)
    assert session is not None
    assert session.pseudonym.value == "Anon-2"


async def test_start_outside_private_chat_is_ignored() -> None:
    handlers, _ = make_handlers()
    context, bot = tg.make_context()
    await handlers.on_start(tg.command_update(tg.message(chat=tg.GROUP, text="/start")), context)
    assert tg.sent_texts(bot) == []


async def test_dm_is_transcribed_under_the_session_pseudonym() -> None:
    handlers, publisher = make_handlers()
    context, _ = tg.make_context()
    await handlers.on_dm(dm("hello there"), context)

    (page, heading, text, _) = publisher.continued[0]
    assert page == "Meta:Community/Discussions"
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
    assert publisher.continued == []


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
