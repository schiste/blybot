"""Shared Telegram-adapter helpers: chat scope and topic-routed replies."""

from __future__ import annotations

from typing import TYPE_CHECKING, Final

from telegram.constants import ChatType

if TYPE_CHECKING:
    from telegram import Bot, Update

GROUP_TYPES: Final = frozenset({ChatType.GROUP, ChatType.SUPERGROUP})


def thread_of(update: Update) -> int:
    """The forum topic the update was sent in; 0 for General/non-forum.

    Gated on ``is_topic_message`` so a reply chain in a non-forum
    supergroup (which also populates ``message_thread_id``) is not
    mistaken for a topic.
    """
    message = update.effective_message
    if message is None or not message.is_topic_message:
        return 0
    return message.message_thread_id or 0


async def send_threaded(bot: Bot, chat_id: int, thread_id: int, text: str) -> None:
    """Send ``text`` to a chat, routed into its forum topic when set."""
    await bot.send_message(chat_id=chat_id, text=text, message_thread_id=thread_id or None)
