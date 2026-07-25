"""Capture boundary: channel posts and opted-in group chatter (v3 §2.7).

This module and the admin ``/capture`` command are the only places that
may read ``from_user`` for archival purposes: the author is reduced to a
stable per-scope HMAC pseudonym label *here*, before anything crosses
into the services layer. Everything else — policy, guards, storage —
lives behind :class:`~blybot.services.capture.CaptureService`.

Broadcast channels opt in structurally: when the bot is made a channel
admin it posts a permanent announcement and enables capture for that
channel; groups opt in explicitly via ``/capture on``.
"""

from __future__ import annotations

import hashlib
import hmac
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final

from telegram.constants import ChatMemberStatus, ChatType
from telegram.error import TelegramError

from blybot.adapters.telegram._common import thread_of
from blybot.domain.models import CapturedMessage
from blybot.domain.ports import StorageError
from blybot.observability import log_event

if TYPE_CHECKING:
    from telegram import Message, Update
    from telegram.ext import ContextTypes

    from blybot.services.capture import CaptureService
    from blybot.services.directory import ChannelDirectory
    from blybot.services.policy import GroupPolicy

CHANNEL_ANNOUNCEMENT: Final = (
    "📢 This channel's posts are now being archived by {bot_name} for "
    "on-wiki summaries and statistics. Authors are recorded only as "
    "anonymous labels; the archive powers scheduled digests published "
    "publicly on Meta-wiki."
)

_LABEL_CHARS: Final = 12  # 48 bits of the HMAC: no realistic collisions per scope


@dataclass(frozen=True)
class HmacAuthorMasker:
    """Derives stable per-scope pseudonym labels from an operator key.

    The label is HMAC-SHA256(key, scope‖user) truncated for readability:
    stable within a scope (so activity stats work), unlinkable across
    scopes, and unlinkable to the account without the key. Rotating
    ``ARCHIVE_PSEUDONYM_KEY`` re-keys everyone at once.
    """

    key: str

    def mask(self, chat_id: int, thread_id: int, author_ref: int) -> str:
        """Return the pseudonym label for this scope's author reference."""
        payload = f"{chat_id}:{thread_id}:{author_ref}".encode()
        digest = hmac.new(self.key.encode(), payload, hashlib.sha256)
        return digest.hexdigest()[:_LABEL_CHARS]


@dataclass(eq=False)
class CaptureHandlers:
    """Update handlers feeding the capture service."""

    service: CaptureService
    masker: HmacAuthorMasker
    directory: ChannelDirectory
    groups: GroupPolicy
    bot_name: str

    async def on_channel_post(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Archive one broadcast-channel post (author-less by nature)."""
        del context
        post = update.channel_post
        if post is None or not self.groups.is_allowed(post.chat.id):
            return
        await self.service.ingest(_as_captured(post, thread_id=0, author=""))

    async def on_group_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Archive one group message for a capture-enabled scope."""
        del context
        message = update.effective_message
        if message is None or not self.groups.is_allowed(message.chat.id):
            return
        thread_id = thread_of(update)
        author = ""
        if message.from_user is not None:
            author = self.masker.mask(message.chat.id, thread_id, message.from_user.id)
        await self.service.ingest(_as_captured(message, thread_id=thread_id, author=author))

    async def on_my_chat_member(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Enable capture and announce it when the bot becomes a channel admin."""
        change = update.my_chat_member
        if change is None or change.chat.type != ChatType.CHANNEL:
            return
        if not self.groups.is_allowed(change.chat.id):
            return
        if change.new_chat_member.status != ChatMemberStatus.ADMINISTRATOR:
            return
        if change.old_chat_member.status == ChatMemberStatus.ADMINISTRATOR:
            return  # a permissions edit, not a promotion: don't re-announce
        # Announce first: loud opt-in is a hard requirement (R-v3.1), so
        # if the channel cannot be told, capture must not start. A
        # demote + re-promote retries the whole sequence.
        try:
            await context.bot.send_message(
                chat_id=change.chat.id,
                text=CHANNEL_ANNOUNCEMENT.format(bot_name=self.bot_name),
            )
        except TelegramError:
            log_event("capture_announce", "error")
            return
        try:
            await self.directory.set_capture(change.chat.id, 0, enabled=True)
        except StorageError:
            # Announced but not enabled — the safe direction to fail.
            log_event("capture_enable", "error")
            return
        self.service.forget_scope(change.chat.id, 0)
        log_event("capture_enable", "ok")


def _as_captured(message: Message, thread_id: int, author: str) -> CapturedMessage:
    text = message.text or message.caption or ""
    return CapturedMessage(
        chat_id=message.chat.id,
        thread_id=thread_id,
        message_id=message.message_id,
        posted_at=message.date,
        author=author,
        kind="text" if text else "media_note",
        text=text,
        reply_to=(
            message.reply_to_message.message_id if message.reply_to_message is not None else None
        ),
    )
