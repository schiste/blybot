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
    from telegram import Chat, ChatMemberUpdated, Message, Update
    from telegram.ext import ContextTypes

    from blybot.domain.models import Session
    from blybot.services.policy import GroupPolicy, SlidingWindowLimiter
    from blybot.services.publish import LogPublicationService
    from blybot.services.sessions import SessionRegistry
    from blybot.services.transcribe import DmTranscriptionService

REPLY_USAGE: Final = "Reply to a text message with /log to publish it anonymously."
REPLY_LOG_IS_GROUP_ONLY: Final = (
    "/log works in groups: reply there to the message you want published. "
    "Here in private you don't need it — everything you write to me "
    "(except commands) is already transcribed anonymously. See /help."
)
REPLY_MEDIA_DECLINED: Final = (
    "That message has no text I can publish — media is not supported (yet)."
)
REPLY_PUBLISHED: Final = "Published anonymously to {page}."
REPLY_THROTTLED: Final = "Rate limit reached — please try again in a minute."
REPLY_WIKI_ERROR: Final = "Sorry, publishing failed. The operator can see details in the logs."
REPLY_AUTHOR_ONLY: Final = "This group's consent policy only lets authors /log their own messages."
REPLY_SESSION_INFO: Final = (
    "You are appearing as {pseudonym}; this exchange is recorded at {page}. "
    "Send /flush any time for a fresh identity."
)
REPLY_FLUSHED: Final = "Fresh identity minted. "
REPLY_NO_SESSION: Final = (
    "You have no active session. Your next message will mint a fresh pseudonym automatically."
)
NEWCOMER_PROMPT: Final = "Welcome! Tap below for a private note on how I work."
NEWCOMER_BUTTON: Final = "What is this bot?"
HELP_PRIVATE: Final = (
    "Anything you write to me here is transcribed to a public Meta-wiki page "
    "under a random per-session pseudonym.\n\n"
    "/whoami — show the pseudonym you currently appear as\n"
    "/flush — discard it and mint a fresh, unlinkable one\n"
    "/privacy — what I collect and publish\n"
    "/help — this message\n\n"
    "In groups, reply to a message with /log to publish it anonymously."
)
HELP_GROUP: Final = (
    "Reply to a message with /log to publish it anonymously to the Meta-wiki "
    "log. Message me privately for anonymous transcription — /help there for details."
)
PRIVACY_TEXT: Final = (
    "What I ingest: only messages explicitly marked with /log in groups, and "
    "what you send me in this private chat. Telegram's privacy mode means "
    "ordinary group chatter is never even delivered to me.\n\n"
    "What I publish: sanitized text on public Meta-wiki pages — permanently. "
    "/log entries carry no attribution at all; private messages appear under "
    "a random per-session pseudonym that is never derived from your account.\n\n"
    "What I store: nothing. No user IDs, usernames, or message archives; "
    "sessions live only in memory and vanish on timeout, /flush, or restart. "
    "My operational logs contain no content and no identifiers.\n\n"
    "What I cannot protect: content that identifies you in its own words, "
    "and the wiki's public edit timestamps."
)

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


def _just_joined(change: ChatMemberUpdated) -> bool:
    """Whether this membership update is a fresh join (not a leave/promotion)."""
    return (
        change.old_chat_member.status not in _MEMBER_STATUSES
        and change.new_chat_member.status in _MEMBER_STATUSES
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
        if message is None or chat is None:
            return
        if chat.type not in _GROUP_TYPES:
            if chat.type == ChatType.PRIVATE:
                # Silence here reads as breakage; explain the gesture instead.
                await context.bot.send_message(chat_id=chat.id, text=REPLY_LOG_IS_GROUP_ONLY)
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
        if not _just_joined(change) or not self.groups.is_allowed(change.chat.id):
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

    async def on_help(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Explain the /log gesture when asked in a served group."""
        chat = update.effective_chat
        if chat is None or chat.type not in _GROUP_TYPES or not self.groups.is_allowed(chat.id):
            return
        await context.bot.send_message(chat_id=chat.id, text=HELP_GROUP)

    async def on_newcomer(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Offer newcomers a private welcome via a deep-link button (R5).

        The bot never DMs anyone unprompted — a private chat only opens
        when the newcomer taps the button and presses Start themselves,
        which is both the Telegram constraint and the privacy stance.
        """
        change = update.chat_member
        if change is None or change.chat.type not in _GROUP_TYPES:
            return
        if not _just_joined(change) or not self.groups.is_allowed(change.chat.id):
            return
        if change.new_chat_member.user.is_bot:
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
        """Deliver the welcome message (R5) — nothing else.

        ``/start`` arrives automatically when someone opens the chat
        (including via the newcomer deep link). It neither mints nor
        resets an identity: sessions are created lazily by the first
        transcribed message, and rotation is the explicit ``/flush``.
        """
        chat = self._private_chat(update)
        if chat is not None:
            await context.bot.send_message(chat_id=chat.id, text=self.welcome_text)
            log_event("welcome_delivered", "ok")

    async def on_flush(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Discard the current pseudonym and mint a fresh one (spec 10).

        This is the user-facing unlinkability boundary: nothing ties
        what was said before a ``/flush`` to what is said after it.
        """
        chat = self._private_chat(update)
        if chat is None:
            return
        session = self.sessions.reset(chat.id)
        notice = self._opened_session_notice(session)
        await context.bot.send_message(chat_id=chat.id, text=f"{REPLY_FLUSHED}{notice}")

    async def on_whoami(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Disclose the current pseudonym without rotating it."""
        chat = self._private_chat(update)
        if chat is None:
            return
        session = self.sessions.peek(chat.id)
        if session is None:
            await context.bot.send_message(chat_id=chat.id, text=REPLY_NO_SESSION)
            return
        info = REPLY_SESSION_INFO.format(
            pseudonym=session.pseudonym.value, page=self.transcription.page_for(session)
        )
        await context.bot.send_message(chat_id=chat.id, text=info)

    async def on_help(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """List the private-chat commands and what transcription means."""
        chat = self._private_chat(update)
        if chat is not None:
            await context.bot.send_message(chat_id=chat.id, text=HELP_PRIVATE)

    async def on_privacy(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """State exactly what is ingested, published, and stored."""
        chat = self._private_chat(update)
        if chat is not None:
            await context.bot.send_message(chat_id=chat.id, text=PRIVACY_TEXT)

    async def on_dm(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Transcribe one private message under the session pseudonym (R4)."""
        message = update.effective_message
        chat = self._private_chat(update)
        if chat is None:
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
            notice = self._opened_session_notice(session)
            await context.bot.send_message(chat_id=chat.id, text=notice)

    def _opened_session_notice(self, session: Session) -> str:
        """Count and log a session opening; return the user-facing notice."""
        self.counters.increment("sessions_opened")
        log_event("session_opened", "ok")
        return REPLY_SESSION_INFO.format(
            pseudonym=session.pseudonym.value, page=self.transcription.page_for(session)
        )

    @staticmethod
    def _private_chat(update: Update) -> Chat | None:
        """Return the chat if this update came from a private chat."""
        chat = update.effective_chat
        if chat is None or chat.type != ChatType.PRIVATE:
            return None
        return chat
