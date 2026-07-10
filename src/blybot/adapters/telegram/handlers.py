"""Telegram update handlers (spec R1-R3, 8, 15).

This module is the anonymity boundary (R6): handlers read Telegram
updates, extract **message text only**, and delegate to services. The
one place identifiers are touched — the author check for
``CONSENT_MODE=author_only`` and the throttle keys — compares/holds
them transiently in memory and never logs or forwards them.

Privacy mode (R1) shapes what ever arrives here: in groups the bot
receives only commands addressed to it (with ``reply_to_message``
attached) and service messages; ordinary chatter is never delivered.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Final

from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ChatMemberStatus, ChatType

from blybot.domain.models import ConsentMode
from blybot.domain.ports import WikiWriteError
from blybot.observability import Counters, log_event
from blybot.services.publish import NothingToPublishError

if TYPE_CHECKING:
    from telegram import Message, Update
    from telegram.ext import ContextTypes

    from blybot.services.policy import GroupPolicy, SlidingWindowLimiter
    from blybot.services.publish import LogPublicationService
    from blybot.services.sessions import SessionRegistry
    from blybot.services.transcribe import DmTranscriptionService

REPLY_USAGE: Final = "Reply to a text message with /log to publish it anonymously."
REPLY_MEDIA_DECLINED: Final = (
    "That message has no text I can publish — media is not supported (yet)."
)
REPLY_PUBLISHED: Final = "Published anonymously to {page}."
REPLY_THROTTLED: Final = "Rate limit reached — please try again in a minute."
REPLY_WIKI_ERROR: Final = "Sorry, publishing failed. The operator can see details in the logs."
REPLY_AUTHOR_ONLY: Final = "This group's consent policy only lets authors /log their own messages."
REPLY_SESSION_INFO: Final = (
    "You are appearing as {pseudonym}; this exchange is recorded at {page}. "
    "Send /start any time for a fresh identity."
)
NEWCOMER_PROMPT: Final = "Welcome! Tap below for a private note on how I work."
NEWCOMER_BUTTON: Final = "What is this bot?"

_GROUP_TYPES: Final = frozenset({ChatType.GROUP, ChatType.SUPERGROUP})
_MEMBER_STATUSES: Final = frozenset(
    {ChatMemberStatus.MEMBER, ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.OWNER}
)


def _same_author(command: Message, target: Message) -> bool:
    """Whether the ``/log`` sender authored the target message.

    The user-id comparison happens transiently in memory and is never
    logged or persisted (R6).
    """
    return (
        command.from_user is not None
        and target.from_user is not None
        and command.from_user.id == target.from_user.id
    )


@dataclass(eq=False)
class GroupHandlers:
    """Handlers for the group ``/log`` flow, greeting, and migration."""

    log_service: LogPublicationService
    groups: GroupPolicy
    limiter: SlidingWindowLimiter
    consent_mode: ConsentMode
    counters: Counters
    group_greeting_text: str
    log_page: str

    async def on_log(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Publish the replied-to message anonymously (R2)."""
        message = update.effective_message
        chat = update.effective_chat
        if message is None or chat is None or chat.type not in _GROUP_TYPES:
            return
        if not self.groups.is_allowed(chat.id):
            log_event("log_command", "ignored")
            return

        async def reply(text: str) -> None:
            await context.bot.send_message(chat_id=chat.id, text=text)

        target = message.reply_to_message
        if target is None:
            await reply(REPLY_USAGE)
            return
        if self.consent_mode is ConsentMode.AUTHOR_ONLY and not _same_author(message, target):
            # N1 hook: ConsentMode.CONFIRM would branch here into a
            # DM-confirmation flow; configuration rejects it until built.
            self.counters.increment("log_declined_consent")
            await reply(REPLY_AUTHOR_ONLY)
            return
        if not self._within_rate_limits(message, chat.id):
            self.counters.increment("log_throttled")
            await reply(REPLY_THROTTLED)
            return

        try:
            await self.log_service.publish(target.text)
        except NothingToPublishError:
            self.counters.increment("log_declined_media")
            await reply(REPLY_MEDIA_DECLINED)
        except WikiWriteError:
            await reply(REPLY_WIKI_ERROR)
        else:
            log_event("log_command", "ok")
            await reply(REPLY_PUBLISHED.format(page=self.log_page))

    async def on_my_chat_member(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Greet once when added to a group (R3)."""
        change = update.my_chat_member
        if change is None or change.chat.type not in _GROUP_TYPES:
            return
        was_in = change.old_chat_member.status in _MEMBER_STATUSES
        is_in = change.new_chat_member.status in _MEMBER_STATUSES
        if was_in or not is_in or not self.groups.is_allowed(change.chat.id):
            return
        await context.bot.send_message(chat_id=change.chat.id, text=self.group_greeting_text)
        log_event("greeting", "ok")

    async def on_migration(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Track supergroup upgrades so the allowlist keeps working (spec 8)."""
        del context  # service message only; nothing to send
        message = update.effective_message
        if message is None or message.migrate_to_chat_id is None:
            return
        applied = self.groups.migrate(message.chat.id, message.migrate_to_chat_id)
        log_event("chat_migration", "ok" if applied else "ignored")

    def _within_rate_limits(self, message: Message, chat_id: int) -> bool:
        if not self.limiter.allow("group", chat_id):
            return False
        user = message.from_user
        return user is None or self.limiter.allow("user", user.id)

    async def on_newcomer(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Offer newcomers a private welcome via a deep-link button (R5).

        The bot never DMs anyone unprompted — a private chat only opens
        when the newcomer taps the button and presses Start themselves,
        which is both the Telegram constraint and the privacy stance.
        """
        change = update.chat_member
        if change is None or change.chat.type not in _GROUP_TYPES:
            return
        if not self.groups.is_allowed(change.chat.id):
            return
        was_in = change.old_chat_member.status in _MEMBER_STATUSES
        is_in = change.new_chat_member.status in _MEMBER_STATUSES
        if was_in or not is_in or change.new_chat_member.user.is_bot:
            return
        deep_link = f"https://t.me/{context.bot.username}?start=welcome"
        button = InlineKeyboardButton(text=NEWCOMER_BUTTON, url=deep_link)
        await context.bot.send_message(
            chat_id=change.chat.id,
            text=NEWCOMER_PROMPT,
            reply_markup=InlineKeyboardMarkup([[button]]),
        )
        log_event("newcomer_prompt", "ok")


@dataclass(eq=False)
class PrivateHandlers:
    """Handlers for pseudonymous DM sessions (R4, R5)."""

    transcription: DmTranscriptionService
    sessions: SessionRegistry
    counters: Counters
    welcome_text: str

    async def on_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Deliver the welcome and open a fresh pseudonymous session (R4, R5).

        ``/start`` always mints a new identity (spec 10) — both on first
        contact (including the ``?start=welcome`` deep link) and when a
        returning user wants to shed their current pseudonym.
        """
        chat = update.effective_chat
        if chat is None or chat.type != ChatType.PRIVATE:
            return
        session = self.sessions.reset(chat.id)
        self.counters.increment("sessions_opened")
        log_event("session_opened", "ok")
        info = REPLY_SESSION_INFO.format(
            pseudonym=session.pseudonym.value, page=self.transcription.page_for(session)
        )
        await context.bot.send_message(chat_id=chat.id, text=f"{self.welcome_text}\n\n{info}")

    async def on_dm(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Transcribe one private message under the session pseudonym (R4)."""
        message = update.effective_message
        chat = update.effective_chat
        if chat is None or chat.type != ChatType.PRIVATE:
            return
        if message is None or not message.text:
            return
        is_new_session = self.sessions.peek(chat.id) is None
        try:
            session = await self.transcription.record(chat.id, message.text)
        except WikiWriteError:
            await context.bot.send_message(chat_id=chat.id, text=REPLY_WIKI_ERROR)
            return
        if is_new_session:
            # Sessions can also start (or roll over) mid-conversation;
            # tell the user which identity their words appear under.
            self.counters.increment("sessions_opened")
            log_event("session_opened", "ok")
            info = REPLY_SESSION_INFO.format(
                pseudonym=session.pseudonym.value, page=self.transcription.page_for(session)
            )
            await context.bot.send_message(chat_id=chat.id, text=info)
