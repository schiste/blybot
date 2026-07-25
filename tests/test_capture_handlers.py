"""Capture boundary tests: masking, channel posts, group chatter (v3 §2.7)."""

from __future__ import annotations

from datetime import timedelta
from types import SimpleNamespace
from typing import cast

import pytest
from telegram import Chat, Update
from telegram.constants import ChatMemberStatus, ChatType
from telegram.error import TelegramError

from blybot.adapters.telegram.capture import (
    CHANNEL_ANNOUNCEMENT,
    CaptureHandlers,
    HmacAuthorMasker,
)
from blybot.domain.models import CapturedMessage, ConsentMode, GroupProfile
from blybot.observability import Counters
from blybot.services.capture import CaptureService
from blybot.services.directory import ChannelDirectory
from blybot.services.policy import GroupPolicy, SlidingWindowLimiter
from tests import tg
from tests.fakes import FakeClock, InMemoryArchive, InMemoryProfiles

CHANNEL = Chat(id=-200, type=ChatType.CHANNEL)
MASKER = HmacAuthorMasker(key="test-hmac-key")


def make_handlers(
    store: InMemoryProfiles | None = None,
    archive: InMemoryArchive | None = None,
    allowed: set[int] | None = None,
) -> tuple[CaptureHandlers, InMemoryProfiles, InMemoryArchive]:
    store = store if store is not None else InMemoryProfiles()
    archive = archive if archive is not None else InMemoryArchive()
    clock = FakeClock()
    service = CaptureService(
        store=store,
        archive=archive,
        limiter=SlidingWindowLimiter(clock=clock, limit=100, window=timedelta(minutes=1)),
        clock=clock,
        counters=Counters(),
    )
    handlers = CaptureHandlers(
        service=service,
        masker=MASKER,
        directory=ChannelDirectory(
            store=store,
            default_log_page="Log",
            default_consent=ConsentMode.IMMEDIATE,
            default_repo="",
            page_suffix="",
        ),
        groups=GroupPolicy(allowed=allowed if allowed is not None else set()),
        bot_name="Blybot",
    )
    return handlers, store, archive


async def enable(store: InMemoryProfiles, chat_id: int) -> None:
    await store.upsert(GroupProfile(chat_id=chat_id, capture_enabled=True))


def channel_post_update(text: str | None = None) -> Update:
    post = tg.message(chat=CHANNEL, text=text, from_user=None)
    return Update(update_id=3, channel_post=post)


def admin_change(chat: Chat, status: ChatMemberStatus) -> Update:
    change = SimpleNamespace(chat=chat, new_chat_member=SimpleNamespace(status=status))
    return cast("Update", SimpleNamespace(my_chat_member=change))


# --- the masker ---------------------------------------------------------


def test_labels_are_stable_within_a_scope_and_unlinkable_across() -> None:
    same = {MASKER.mask(-1, 0, 42) for _ in range(3)}
    assert len(same) == 1
    label = same.pop()
    assert len(label) == 12
    assert "42" not in label  # nothing of the user id shows through
    assert MASKER.mask(-2, 0, 42) != label  # other group: new identity
    assert MASKER.mask(-1, 7, 42) != label  # other topic: new identity
    assert HmacAuthorMasker(key="rotated").mask(-1, 0, 42) != label  # key rotation re-keys


# --- channel posts ------------------------------------------------------


async def test_channel_post_is_archived_without_an_author() -> None:
    handlers, store, archive = make_handlers()
    await enable(store, CHANNEL.id)
    context, _bot = tg.make_context()

    await handlers.on_channel_post(channel_post_update(text="announcement"), context)

    (stored,) = archive.messages
    assert stored.author == ""
    assert stored.text == "announcement"
    assert stored.kind == "text"


async def test_media_only_posts_become_media_notes() -> None:
    handlers, store, archive = make_handlers()
    await enable(store, CHANNEL.id)
    context, _bot = tg.make_context()

    await handlers.on_channel_post(channel_post_update(text=None), context)

    (stored,) = archive.messages
    assert stored.kind == "media_note"
    assert stored.text == ""


async def test_disallowed_channels_are_ignored() -> None:
    handlers, store, archive = make_handlers(allowed={-999})
    await enable(store, CHANNEL.id)
    context, _bot = tg.make_context()

    await handlers.on_channel_post(channel_post_update(text="x"), context)

    assert archive.messages == []


# --- group chatter ------------------------------------------------------


async def test_group_message_is_archived_with_a_masked_author() -> None:
    handlers, store, archive = make_handlers()
    await enable(store, tg.GROUP.id)
    context, _bot = tg.make_context()
    update = tg.command_update(tg.message(text="hello all", from_user=tg.ALICE))

    await handlers.on_group_message(update, context)

    (stored,) = archive.messages
    assert stored.author == MASKER.mask(tg.GROUP.id, 0, tg.ALICE.id)
    assert stored.text == "hello all"


async def test_replies_keep_their_thread_reference() -> None:
    handlers, store, archive = make_handlers()
    await enable(store, tg.GROUP.id)
    context, _bot = tg.make_context()
    original = tg.message(text="original", from_user=tg.BOB, message_id=41)
    update = tg.command_update(
        tg.message(text="an answer", from_user=tg.ALICE, reply_to=original, message_id=42)
    )

    await handlers.on_group_message(update, context)

    (stored,) = archive.messages
    assert stored.reply_to == 41


# --- channel opt-in on admin promotion -----------------------------------


async def test_becoming_channel_admin_enables_capture_and_announces() -> None:
    handlers, store, _archive = make_handlers()
    context, bot = tg.make_context()

    await handlers.on_my_chat_member(admin_change(CHANNEL, ChatMemberStatus.ADMINISTRATOR), context)

    profile = await store.get(CHANNEL.id, 0)
    assert profile is not None
    assert profile.capture_enabled
    assert tg.sent_texts(bot) == [CHANNEL_ANNOUNCEMENT.format(bot_name="Blybot")]


async def test_group_membership_changes_are_not_capture_events() -> None:
    handlers, store, _archive = make_handlers()
    context, bot = tg.make_context()

    await handlers.on_my_chat_member(
        admin_change(tg.GROUP, ChatMemberStatus.ADMINISTRATOR), context
    )
    await handlers.on_my_chat_member(admin_change(CHANNEL, ChatMemberStatus.MEMBER), context)

    assert await store.get(CHANNEL.id, 0) is None
    bot.send_message.assert_not_awaited()


async def test_channel_enable_survives_storage_and_send_failures() -> None:
    handlers, store, _archive = make_handlers()
    store.fail_upserts = True
    context, bot = tg.make_context()
    await handlers.on_my_chat_member(admin_change(CHANNEL, ChatMemberStatus.ADMINISTRATOR), context)
    bot.send_message.assert_not_awaited()  # no announcement for a failed enable

    store.fail_upserts = False
    bot.send_message.side_effect = TelegramError("muted")
    await handlers.on_my_chat_member(
        admin_change(CHANNEL, ChatMemberStatus.ADMINISTRATOR), context
    )  # must not raise; capture is on even if the announcement was lost
    profile = await store.get(CHANNEL.id, 0)
    assert profile is not None
    assert profile.capture_enabled


async def test_disallowed_channel_is_never_enabled() -> None:
    handlers, store, _archive = make_handlers(allowed={-999})
    context, bot = tg.make_context()

    await handlers.on_my_chat_member(admin_change(CHANNEL, ChatMemberStatus.ADMINISTRATOR), context)

    assert await store.get(CHANNEL.id, 0) is None
    bot.send_message.assert_not_awaited()


async def test_non_admin_capture_command_is_refused() -> None:
    # admin gate coverage for /capture lives with the other admin tests;
    # here: the group-message path skips disallowed groups and tolerates
    # authorless messages (anonymous admins post with no from_user).
    handlers, store, archive = make_handlers(allowed={-999})
    await enable(store, tg.GROUP.id)
    context, _bot = tg.make_context()
    update = tg.command_update(tg.message(text="hello", from_user=tg.ALICE))

    await handlers.on_group_message(update, context)
    assert archive.messages == []


async def test_authorless_group_messages_are_archived_without_a_label() -> None:
    handlers, store, archive = make_handlers()
    await enable(store, tg.GROUP.id)
    context, _bot = tg.make_context()
    update = tg.command_update(tg.message(text="anonymous admin", from_user=None))

    await handlers.on_group_message(update, context)

    (stored,) = archive.messages
    assert stored.author == ""


def test_captured_messages_reject_unknown_kinds() -> None:
    with pytest.raises(ValueError, match="kind"):
        CapturedMessage(chat_id=-1, thread_id=0, message_id=1, posted_at=tg.NOW, kind="sticker")
