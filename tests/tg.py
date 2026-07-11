"""Helpers to build real python-telegram-bot objects for handler tests."""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, cast
from unittest.mock import AsyncMock

from telegram import (
    Chat,
    ChatMemberLeft,
    ChatMemberMember,
    ChatMemberUpdated,
    Message,
    Update,
    User,
)
from telegram.constants import ChatType

if TYPE_CHECKING:
    from telegram.ext import ContextTypes

NOW = datetime(2026, 7, 10, 12, 0, tzinfo=UTC)

GROUP = Chat(id=-100500, type=ChatType.SUPERGROUP)
PRIVATE = Chat(id=777, type=ChatType.PRIVATE)

ALICE = User(id=1, first_name="Alice", is_bot=False)
BOB = User(id=2, first_name="Bob", is_bot=False)


def make_context(args: list[str] | None = None) -> tuple[ContextTypes.DEFAULT_TYPE, AsyncMock]:
    """Return a (context, bot) pair; the bot records outgoing calls."""
    bot = AsyncMock()
    bot.username = "blybot_bot"
    context = cast("ContextTypes.DEFAULT_TYPE", SimpleNamespace(bot=bot, args=args or []))
    return context, bot


def message(
    chat: Chat = GROUP,
    text: str | None = None,
    from_user: User | None = ALICE,
    reply_to: Message | None = None,
    thread_id: int | None = None,
    **extra: Any,
) -> Message:
    return Message(
        message_id=extra.pop("message_id", 10),
        date=NOW,
        chat=chat,
        from_user=from_user,
        text=text,
        reply_to_message=reply_to,
        message_thread_id=thread_id,
        **extra,
    )


def sent_calls(bot: AsyncMock) -> list[tuple[str, int | None]]:
    """(text, message_thread_id) for each send — for topic-routing asserts."""
    return [
        (call.kwargs["text"], call.kwargs.get("message_thread_id"))
        for call in bot.send_message.await_args_list
    ]


def command_update(command_message: Message) -> Update:
    return Update(update_id=1, message=command_message)


def membership_update(
    chat: Chat,
    *,
    user: User,
    joined: bool,
    mine: bool,
) -> Update:
    """An Update for the bot's own membership (mine=True) or a newcomer's."""
    old, new = (
        (ChatMemberLeft(user=user), ChatMemberMember(user=user))
        if joined
        else (ChatMemberMember(user=user), ChatMemberLeft(user=user))
    )
    change = ChatMemberUpdated(
        chat=chat, from_user=user, date=NOW, old_chat_member=old, new_chat_member=new
    )
    if mine:
        return Update(update_id=2, my_chat_member=change)
    return Update(update_id=2, chat_member=change)


def sent_texts(bot: AsyncMock) -> list[str]:
    """All message texts the handler sent through the bot."""
    return [call.kwargs["text"] for call in bot.send_message.await_args_list]
