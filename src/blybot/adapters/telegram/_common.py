"""Shared Telegram-adapter helpers: chat scope and topic-routed replies.

This module is one of the two int↔:class:`Scope` edges. Inbound, a
Telegram ``(chat_id, thread_id)`` pair becomes a ``Scope`` via
:func:`scope_of` / :func:`dm_scope`; outbound, a ``Scope`` becomes the
pair again via :func:`telegram_target`. Everything past this boundary
speaks ``Scope`` only.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Final

from telegram.constants import ChatType

from blybot.domain.models import Scope

if TYPE_CHECKING:
    from telegram import Bot, Update

GROUP_TYPES: Final = frozenset({ChatType.GROUP, ChatType.SUPERGROUP})

PLATFORM: Final = "telegram"


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


def scope_of(update: Update) -> Scope:
    """Build the :class:`Scope` addressing the chat/topic ``update`` came from."""
    chat = update.effective_chat
    chat_id = chat.id if chat is not None else 0
    thread = thread_of(update)
    return Scope(PLATFORM, str(chat_id), str(thread) if thread else "")


def group_scope(chat_id: int) -> Scope:
    """A thread-less :class:`Scope` for a group chat id (allowlist checks, migration)."""
    return Scope(PLATFORM, str(chat_id))


def dm_scope(chat_id: int) -> Scope:
    """A :class:`Scope` addressing a private (DM) chat — its channel is the chat id."""
    return Scope(PLATFORM, str(chat_id))


def telegram_target(scope: Scope) -> tuple[int, int | None]:
    """Convert a :class:`Scope` back into a Telegram ``(chat_id, thread_id)`` pair."""
    return int(scope.channel), int(scope.thread) if scope.thread else None


async def send_threaded(bot: Bot, chat_id: int, thread_id: int, text: str) -> None:
    """Send ``text`` to a chat, routed into its forum topic when set."""
    await bot.send_message(chat_id=chat_id, text=text, message_thread_id=thread_id or None)
