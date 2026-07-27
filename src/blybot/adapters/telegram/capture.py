"""Capture boundary: channel posts and opted-in group chatter (v3 §2.7).

This module and the admin ``/capture`` command are the only places that
may read ``from_user`` for archival purposes: the author is reduced to a
stable per-scope HMAC pseudonym label *here*, before anything crosses
into the services layer. Everything else — policy, guards, storage —
lives behind :class:`~blybot.services.capture.CaptureService`.

Broadcast channels opt in structurally: when the bot is made a channel
admin it posts a permanent announcement and enables capture for that
channel, and demoting (or removing) it disables capture again — admin
status *is* the channel's consent. Groups opt in explicitly via
``/capture on``.
"""

from __future__ import annotations

import hashlib
import hmac
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final

from telegram.constants import ChatMemberStatus, ChatType
from telegram.error import TelegramError

from blybot.adapters.telegram._common import group_scope, scope_of, thread_of
from blybot.domain.models import CapturedMessage
from blybot.domain.ports import StorageError
from blybot.observability import log_event

if TYPE_CHECKING:
    from telegram import Message, Update
    from telegram.ext import ContextTypes

    from blybot.domain.models import Scope
    from blybot.services.capture import CaptureService
    from blybot.services.directory import ChannelDirectory
    from blybot.services.policy import GroupPolicy

CHANNEL_ANNOUNCEMENT: Final = (
    "📢 This channel's posts are now being archived by {bot_name} for "
    "on-wiki summaries and statistics. Authors are recorded only as "
    "anonymous labels; the archive powers scheduled digests published "
    "publicly on Meta-wiki."
)
CHANNEL_RETRACTION: Final = (
    "⚠️ Correction: enabling the archive failed, so this channel's posts "
    "are NOT being archived. Demote and re-promote {bot_name} to retry."
)

_LABEL_CHARS: Final = 12  # 48 bits of the HMAC: no realistic collisions per scope


@dataclass(frozen=True)
class HmacAuthorMasker:
    """Derives stable per-scope pseudonym labels from an operator key.

    The label is HMAC-SHA256(key, scope‖user) truncated for readability:
    stable within a scope (so activity stats work), unlinkable across
    scopes, and unlinkable to the account without the key. Rotating
    ``ARCHIVE_PSEUDONYM_KEY`` re-keys every *future* label; archived
    rows keep theirs (nothing stored can recompute them — R-v3.2).
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
        if post is None or not self.groups.is_allowed(group_scope(post.chat.id)):
            return
        await self.service.ingest(_as_captured(post, group_scope(post.chat.id), author=""))

    async def on_group_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Archive one group message for a capture-enabled scope."""
        del context
        message = update.effective_message
        if message is None or not self.groups.is_allowed(group_scope(message.chat.id)):
            return
        thread_id = thread_of(update)
        author = ""
        if message.from_user is not None:
            author = self.masker.mask(message.chat.id, thread_id, message.from_user.id)
        await self.service.ingest(_as_captured(message, scope_of(update), author=author))

    async def on_my_chat_member(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Track the bot's channel admin status — the structural opt-in.

        Admin status *is* a channel's capture consent (channels have no
        `/capture` commands), so the two transitions are symmetric:
        promotion announces and enables, demotion (or removal) disables.
        Every genuine promotion re-announces — a channel that took the
        bot's admin rights away and later restores them gets a fresh
        loud opt-in, never a silent resumption.
        """
        change = update.my_chat_member
        if change is None or change.chat.type != ChatType.CHANNEL:
            return
        admin = ChatMemberStatus.ADMINISTRATOR
        was_admin = change.old_chat_member.status == admin
        is_admin = change.new_chat_member.status == admin
        scope = group_scope(change.chat.id)
        if was_admin and not is_admin:
            # Revocations run regardless of the serving allowlist: consent
            # ends when admin status does, even for a channel the operator
            # currently excludes — otherwise a stale durable enable could
            # resume archiving if the channel is ever re-allowed.
            await self._end_channel_capture(scope)
        elif is_admin and not was_admin and self.groups.is_allowed(scope):
            await self._begin_channel_capture(scope, context)
        # admin→admin is a permissions edit; anything else is not ours.

    async def _end_channel_capture(self, scope: Scope) -> None:
        """Demotion revokes the structural opt-in: capture must not survive it."""
        try:
            await self.directory.set_capture(scope, enabled=False)
        except StorageError:
            # A demoted bot still *receives* channel posts (it stays a
            # member), so the stale durable True must not resume once
            # storage recovers: tombstone the scope — denied in memory,
            # with each incoming post retrying the durable disable.
            self.service.deny_scope(scope)
            log_event("capture_enable", "error")
            return
        self.service.clear_denial(scope)
        log_event("capture_enable", "ok", enabled=0)

    async def _begin_channel_capture(
        self, scope: Scope, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Announce first (loud opt-in, R-v3.1), then enable; never claim falsely."""
        try:
            await context.bot.send_message(
                chat_id=int(scope.channel),
                text=CHANNEL_ANNOUNCEMENT.format(bot_name=self.bot_name),
            )
        except TelegramError:
            # The channel cannot be told, so capture must not start — and
            # any stale enabled state (a demotion the store never
            # recorded) must not survive either. A demote + re-promote
            # retries the whole sequence.
            log_event("capture_announce", "error")
            await self._end_channel_capture(scope)
            return
        try:
            # Serialized against the tombstone retries: a stale in-flight
            # disable can never overwrite this freshly announced consent.
            await self.service.enable_scope(scope)
        except StorageError:
            log_event("capture_enable", "error")
            await self._retract_if_verifiably_off(scope, context)
            return
        log_event("capture_enable", "ok")

    async def _retract_if_verifiably_off(
        self, scope: Scope, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """After a failed enable write, correct the announcement — but only truthfully.

        A failed write is an *uncertain* outcome (the UPSERT may have
        committed before the connection dropped), and older state may
        even already be enabled. Force the state off; only when that
        write is confirmed may the "NOT being archived" correction be
        posted. If even the disable fails, the state is unknowable — the
        standing announcement then over-warns (claims archiving that may
        not happen), which is the privacy-safe direction to be wrong in.
        """
        try:
            await self.directory.set_capture(scope, enabled=False)
        except StorageError:
            # Unknowable durable state: tombstone it fail-closed.
            self.service.deny_scope(scope)
            log_event("capture_enable", "error")
            return
        self.service.clear_denial(scope)
        try:
            await context.bot.send_message(
                chat_id=int(scope.channel),
                text=CHANNEL_RETRACTION.format(bot_name=self.bot_name),
            )
        except TelegramError:
            log_event("capture_announce", "error")


def _as_captured(message: Message, scope: Scope, author: str) -> CapturedMessage:
    text = message.text or message.caption or ""
    return CapturedMessage(
        scope=scope,
        message_id=message.message_id,
        posted_at=message.date,
        author=author,
        kind="text" if text else "media_note",
        text=text,
        reply_to=(
            message.reply_to_message.message_id if message.reply_to_message is not None else None
        ),
    )
