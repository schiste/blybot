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

import asyncio
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Final

from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    KeyboardButtonRequestChat,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
)
from telegram.constants import ChatMemberStatus, ChatType
from telegram.error import TelegramError

from blybot.adapters.telegram._common import (
    GROUP_TYPES,
    dm_scope,
    group_scope,
    scope_of,
    send_threaded,
    thread_of,
)
from blybot.domain.models import LogContent, LogMedia, Scope
from blybot.domain.ports import IssueTrackerError, StorageError, WikiWriteError
from blybot.observability import Counters, log_event
from blybot.services import commands as cmd
from blybot.services.feedback import BUG_ACTION
from blybot.services.feedback import CONFIRMATION_TEMPLATE as BUG_CONFIRMATION
from blybot.services.publish import CONFIRMATION_TEMPLATE as PUBLISH_CONFIRMATION
from blybot.services.publish import PublishedLog

if TYPE_CHECKING:
    from collections.abc import Callable

    from telegram import Bot, Chat, ChatMemberUpdated, Message, Update
    from telegram.ext import ContextTypes

    from blybot.adapters.telegram.subscribe import SubscriptionHandlers
    from blybot.adapters.telegram.token_entry import TokenEntryHandler
    from blybot.domain.models import PlatformCapabilities, Session
    from blybot.domain.ports import MessageArchive, SubscriptionStore
    from blybot.services.commands import CommandService
    from blybot.services.directory import ChannelDirectory, ChannelSettings
    from blybot.services.dm_routing import DmRouteRegistry, PendingDmMessages
    from blybot.services.engine import ActionEngine
    from blybot.services.feedback import FeedbackService
    from blybot.services.policy import GroupPolicy, SlidingWindowLimiter
    from blybot.services.sessions import SessionRegistry
    from blybot.services.transcribe import DmTranscriptionService

REPLY_USAGE: Final = "Reply to a text message with /log to publish it anonymously."
REPLY_LOGMEDIA_USAGE: Final = (
    "Reply to a text or image message with /logmedia to upload images too."
)
REPLY_LOG_IS_GROUP_ONLY: Final = (
    "/log works in groups: reply there to the message you want published. "
    "Here in private, just send the text and I'll ask which group page "
    "should receive it. See /help."
)
REPLY_MEDIA_DECLINED: Final = "That message has no text or supported image I can publish."
REPLY_MEDIA_FETCH_FAILED: Final = "Sorry, I couldn't fetch that image from Telegram."
REPLY_PUBLISHED: Final = PUBLISH_CONFIRMATION  # sink-owned copy, re-exported for tests
REPLY_MEDIA_REVIEW_DM: Final = (
    "Media uploaded for review:\n{links}\n\n"
    "Please check the file description and license before {deadline}. "
    "Past that day, unreviewed media can be safely deleted."
)
REPLY_THROTTLED: Final = "Rate limit reached — please try again in a minute."
REPLY_WIKI_ERROR: Final = "Sorry, publishing failed. The operator can see details in the logs."
REPLY_AUTHOR_ONLY: Final = "This group's consent policy only lets authors /log their own messages."
REPLY_SESSION_INFO: Final = (
    "You are appearing as {pseudonym}; this exchange is queued for {page}. "
    "Send /flush any time for a fresh identity."
)
REPLY_FLUSHED: Final = "Fresh identity minted. "
REPLY_NO_SESSION: Final = (
    "You have no active session. Your next message will mint a fresh pseudonym automatically."
)
REPLY_DM_DESTINATION_REQUIRED: Final = (
    "Where should I publish this? Choose a group and I'll queue this message there."
)
REPLY_DM_DESTINATION_EXPIRED: Final = (
    "That destination request expired. Please send the message again."
)
REPLY_DM_DESTINATION_NOT_SERVED: Final = "I don't serve that group."
REPLY_DM_DESTINATION_SET: Final = "Destination set: {page}"
DM_DESTINATION_BUTTON: Final = "Choose group"
REPLY_BUG_USAGE: Final = (
    "Describe the problem after the command, e.g.: /bug the bot ignored my /flush"
)
REPLY_BUG_FILED: Final = BUG_CONFIRMATION  # sink-owned copy, re-exported for tests
REPLY_BUG_DISABLED: Final = "Chat bug reports aren't enabled; please open an issue at {url}"
REPLY_BUG_FAILED: Final = "Sorry, filing the issue failed — please report it at {url}"
REPLY_CONFIG_UNAVAILABLE: Final = (
    "This group's configuration is temporarily unreachable, so I won't "
    "publish right now — please try again shortly."
)
REPLY_NO_LOG_PAGE: Final = (
    "No log page is set for this group. An admin needs to run /setpage "
    "<page path> here to set the group default, or inside a topic to scope "
    "it to that topic. Nothing was published."
)
# /issue and /repo wording (and their error mapping) now live in the neutral
# CommandService — both platforms share it; on_issue/on_repo render whatever
# CommandResult it returns.
NEWCOMER_PROMPT: Final = "Welcome! Tap below for a private note on how I work."
NEWCOMER_BUTTON: Final = "What is this bot?"
HELP_PRIVATE: Final = (
    "Send me text here and I'll ask which shared group should receive it "
    "on Meta-wiki. It appears under a random per-session pseudonym.\n\n"
    "/whoami — show the pseudonym you currently appear as\n"
    "/flush — discard it and mint a fresh, unlinkable one\n"
    "/mysubs — list your digest subscriptions; /unsubscribe <id> to stop one\n"
    "/privacy — what I collect and publish\n"
    "/bug — file an anonymous bug report with my maintainer\n"
    "/help — this message\n\n"
    "To subscribe to a group's digest here, open the link an admin shares after "
    "/subscribable on. In groups, reply to a text message with /log, or use "
    "/logmedia to include images."
)
HELP_GROUP: Final = (
    "Reply to a text message with /log to publish it anonymously to the Meta-wiki "
    "log. Use /logmedia when the image itself should be uploaded too. "
    "Message me privately for anonymous transcription — /help there for details."
)
HELP_GROUP_SELF_SERVICE: Final = (
    " /issue files a bug in this group's repo; /repo shows its status. "
    "Group admins: /setup to configure me."
)
PRIVACY_TEXT: Final = (
    "What I ingest: messages explicitly marked with /log in groups, what "
    "you send me in this private chat, and — ONLY where admins switched on "
    "/capture (announced in the chat) — that channel or group's messages, "
    "archived for on-wiki summaries and statistics.\n\n"
    "What I publish: sanitized text on public Meta-wiki pages — permanently. "
    "/log entries carry no attribution at all; private messages appear under "
    "a random per-session pseudonym that is never derived from your account; "
    "capture-based summaries and statistics name nobody.\n\n"
    "What I store: by default no user IDs or usernames. Sessions live only in "
    "memory and vanish on timeout, /flush, or restart. For groups whose "
    "admins configure me, I keep that group's chat id, its chosen "
    "page/repository, and (encrypted) any API token an admin supplies — "
    "/reset in the group deletes all of it. In capture-enabled chats I "
    "additionally store message text with authors reduced to anonymous "
    "per-chat labels; the archive keeps the first version of a message — "
    "later edits and deletions in Telegram do not change it. /capture off "
    "stops collection and /capture purge erases the archive. One exception "
    "to the no-identifiers rule: if YOU choose to subscribe to a digest here "
    "with /subscribe, I keep one record tying this private chat to the group "
    "and delivery schedule you picked — the only place I durably store a "
    "Telegram user identifier, kept solely to deliver your digest and erased "
    "the moment you /unsubscribe. My operational logs contain no content and "
    "no identifiers.\n\n"
    "What I cannot protect: content that identifies you in its own words, "
    "and the wiki's public edit timestamps.\n\n"
    "How I run: as a continuous job on Wikimedia Toolforge (Kubernetes), "
    "movement-hosted infrastructure — no third-party servers or analytics. "
    "Credentials live in a permission-restricted file on the tool account, and "
    "the only stored state is the group-config row and any digest "
    "subscriptions described above. I am free "
    "software (AGPL-3.0): every line, including this message, is auditable at "
    "https://github.com/schiste/blybot"
)

_MEMBER_STATUSES: Final = frozenset(
    {ChatMemberStatus.MEMBER, ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.OWNER}
)
_SUPPORTED_IMAGE_TYPES: Final = frozenset({"image/jpeg", "image/png", "image/gif", "image/webp"})
# The /bug pipeline runs on a placeholder scope so no private chat id (a
# user identifier) ever enters the engine; the handler routes the reply.
_BUG_SENTINEL_SCOPE: Final = Scope("telegram", "0")


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


def _message_text(message: Message) -> str | None:
    """Return the text-like payload Telegram exposes for a message."""
    return message.text or message.caption


def _media_candidate(message: Message) -> tuple[str, str] | None:
    """Return ``(file_id, content_type)`` for one supported image, if present."""
    if message.photo:
        photo = max(message.photo, key=lambda item: (item.file_size or 0, item.width * item.height))
        return photo.file_id, "image/jpeg"
    document = message.document
    if document is None or document.mime_type not in _SUPPORTED_IMAGE_TYPES:
        return None
    return document.file_id, document.mime_type


async def _log_content_from_message(
    bot: Bot, message: Message, *, include_media: bool
) -> LogContent:
    """Convert a Telegram reply target into anonymous publishable content."""
    if not include_media:
        return LogContent(text=_message_text(message))
    candidate = _media_candidate(message)
    if candidate is None:
        return LogContent(text=_message_text(message))
    file_id, content_type = candidate
    telegram_file = await bot.get_file(file_id)
    content = bytes(await telegram_file.download_as_bytearray())
    return LogContent(text=_message_text(message), media=(LogMedia(content, content_type),))


def _help_footer(page_url: str, maintainer: str) -> str:
    """Publication link and maintainer mention appended to both /help texts."""
    footer = f"\n\nEverything I publish lands at {page_url}"
    if maintainer:
        footer += f"\nThis bot is maintained by {maintainer}"
    return footer


@dataclass(eq=False)
class GroupHandlers:
    """Handlers for the group ``/log`` flow, greeting, and migration."""

    engine: ActionEngine
    groups: GroupPolicy
    limiter: SlidingWindowLimiter
    directory: ChannelDirectory
    page_url_for: Callable[[str], str]
    counters: Counters
    group_greeting_text: str
    maintainer: str
    newcomer_welcome_enabled: bool
    # /issue and /repo are pure delegations to the neutral service, which
    # carries the bound-repo service and their rate cap.
    commands: CommandService
    # Gates the platform-shaped niceties: command-message cleanup needs
    # message_delete, and the newcomer welcome mints a deep link
    # (deep_links). Telegram has both, so neither gate changes its behavior.
    capabilities: PlatformCapabilities
    # Capture-enabled deployments only: the archive follows the group
    # across supergroup migrations, alongside its profiles.
    archive: MessageArchive | None = None
    # Digest subscriptions also re-key on a supergroup upgrade (§21).
    subscriptions: SubscriptionStore | None = None
    # The /log command message is deleted after this delay, hiding who
    # requested the publication. Requires the "Delete messages" admin
    # right; without it the cleanup is skipped silently.
    cleanup_delay_seconds: float = 5.0
    # The bot's own /log replies (confirmation, hints) self-delete after
    # this delay — long enough to read, then the group stays tidy.
    # Deleting its own messages needs no admin right.
    reply_cleanup_delay_seconds: float = 15.0
    _cleanup_tasks: set[asyncio.Task[None]] = field(default_factory=set, init=False)

    async def on_log(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Publish the replied-to message text anonymously (R2)."""
        await self._on_log(update, context, include_media=False)

    async def on_logmedia(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Publish the replied-to message text and supported images anonymously."""
        await self._on_log(update, context, include_media=True)

    async def _on_log(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE, *, include_media: bool
    ) -> None:
        message = update.effective_message
        chat = update.effective_chat
        if message is None or chat is None:
            return
        if chat.type not in GROUP_TYPES:
            if chat.type == ChatType.PRIVATE:
                # Silence here reads as breakage; explain the gesture instead.
                await context.bot.send_message(chat_id=chat.id, text=REPLY_LOG_IS_GROUP_ONLY)
            return
        scope = scope_of(update)
        if not self.groups.is_allowed(scope):
            log_event("log_command", "ignored")
            return
        await self._schedule_cleanup(
            context.bot, chat.id, message.message_id, self.cleanup_delay_seconds
        )
        thread_id = thread_of(update)

        async def reply(text: str) -> None:
            sent = await context.bot.send_message(
                chat_id=chat.id, text=text, message_thread_id=thread_id or None
            )
            await self._schedule_cleanup(
                context.bot, chat.id, sent.message_id, self.reply_cleanup_delay_seconds
            )

        target = message.reply_to_message
        if target is None:
            await reply(REPLY_LOGMEDIA_USAGE if include_media else REPLY_USAGE)
            return
        # The per-USER cap stays here permanently: it keys on the raw Telegram
        # user id, which must never cross inward (the R6 boundary). The
        # per-scope guards, consent policy and publish pipeline are shared.
        if not self._within_user_rate_limit(message):
            self.counters.increment("log_throttled")
            await reply(REPLY_THROTTLED)
            return
        try:
            content = await _log_content_from_message(
                context.bot, target, include_media=include_media
            )
        except TelegramError:
            log_event("telegram_media", "error")
            await reply(REPLY_MEDIA_FETCH_FAILED)
            return
        result = await self.commands.log_message(
            scope, is_author=_same_author(message, target), content=content
        )
        await reply(REPLY_MEDIA_DECLINED if result.text == cmd.REPLY_LOG_NOTHING else result.text)
        if result.ok and include_media and isinstance(result.payload, PublishedLog):
            await self._send_media_review_dm(context.bot, message, result.payload)

    def _within_user_rate_limit(self, message: Message) -> bool:
        """Whether this SENDER is under their personal /log cap.

        The one piece of the /log guards that cannot move into the neutral
        service: it keys on the raw Telegram user id, which the architecture
        guard forbids passing inward. The per-scope cap lives in
        CommandService alongside the rest.
        """
        user = message.from_user
        return user is None or self.limiter.allow("user", str(user.id))

    async def on_my_chat_member(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Greet once when added to a group (R3)."""
        change = update.my_chat_member
        if change is None or change.chat.type not in GROUP_TYPES:
            return
        if not _just_joined(change) or not self.groups.is_allowed(group_scope(change.chat.id)):
            return
        await context.bot.send_message(chat_id=change.chat.id, text=self.group_greeting_text)
        log_event("greeting", "ok")

    async def _send_media_review_dm(
        self, bot: Bot, command: Message, published: PublishedLog
    ) -> None:
        if not published.media or command.from_user is None:
            return
        links = "\n".join(self.page_url_for(f"File:{item.filename}") for item in published.media)
        deadline = published.media[0].review_deadline
        try:
            await bot.send_message(
                chat_id=command.from_user.id,
                text=REPLY_MEDIA_REVIEW_DM.format(links=links, deadline=deadline),
            )
        except TelegramError:
            log_event("media_review_dm", "ignored")

    async def on_migration(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Track supergroup upgrades so the allowlist keeps working (spec 8)."""
        del context  # service message only; nothing to send
        message = update.effective_message
        if message is None or message.migrate_to_chat_id is None:
            return
        old = group_scope(message.chat.id)
        new = group_scope(message.migrate_to_chat_id)
        applied = self.groups.migrate(old, new)
        try:
            await self.directory.migrate(old, new)
            if self.archive is not None:
                # The captured messages move with the profiles: rows left
                # under the dead chat id would vanish from analyses and be
                # unreachable by /capture purge.
                await self.archive.migrate(old, new)
            if self.subscriptions is not None:
                await self.subscriptions.migrate(old, new)
        except StorageError:
            log_event("chat_migration", "error")
            return
        log_event("chat_migration", "ok" if applied else "ignored")

    async def on_issue(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """File an anonymous issue in the group's bound repository."""
        chat = self._served_group(update)
        if chat is None:
            return
        result = await self.commands.file_issue(
            scope_of(update), description=" ".join(context.args or ())
        )
        await send_threaded(context.bot, chat.id, thread_of(update), result.text)

    async def on_repo(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Show the bound repository's open-items summary."""
        chat = self._served_group(update)
        if chat is None:
            return
        result = await self.commands.repo_summary(scope_of(update))
        await send_threaded(context.bot, chat.id, thread_of(update), result.text)

    def _served_group(self, update: Update) -> Chat | None:
        """Return the chat when this is a group the bot serves."""
        chat = update.effective_chat
        if chat is None or chat.type not in GROUP_TYPES:
            return None
        if not self.groups.is_allowed(group_scope(chat.id)):
            return None
        return chat

    async def on_help(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Explain the /log gesture when asked in a served group."""
        chat = self._served_group(update)
        if chat is None:
            return
        thread_id = thread_of(update)
        settings = await self.directory.resolve(scope_of(update))
        page_url = self.page_url_for(settings.log_page)
        text = HELP_GROUP
        if self.directory.self_service_enabled:
            text += HELP_GROUP_SELF_SERVICE
        text += _help_footer(page_url, self.maintainer)
        await send_threaded(context.bot, chat.id, thread_id, text)

    async def _schedule_cleanup(
        self, bot: Bot, chat_id: int, message_id: int, delay_seconds: float
    ) -> None:
        if not self.capabilities.message_delete:  # platform can't delete messages
            return
        if delay_seconds < 0:  # cleanup disabled by configuration
            return
        if delay_seconds == 0:
            await self._delete_after(bot, chat_id, message_id, delay_seconds)
            return
        task = asyncio.get_running_loop().create_task(
            self._delete_after(bot, chat_id, message_id, delay_seconds)
        )
        self._cleanup_tasks.add(task)
        task.add_done_callback(self._cleanup_tasks.discard)

    async def _delete_after(
        self, bot: Bot, chat_id: int, message_id: int, delay_seconds: float
    ) -> None:
        await asyncio.sleep(delay_seconds)
        try:
            await bot.delete_message(chat_id=chat_id, message_id=message_id)
        except TelegramError:
            # Deleting others' messages needs the "Delete messages"
            # admin right; running without it is fine, just untidy.
            log_event("command_cleanup", "ignored")

    async def on_newcomer(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Offer newcomers a private welcome via a deep-link button (R5).

        The bot never DMs anyone unprompted — a private chat only opens
        when the newcomer taps the button and presses Start themselves,
        which is both the Telegram constraint and the privacy stance.
        The whole prompt is an operator switch (NEWCOMER_WELCOME).
        """
        if not self.newcomer_welcome_enabled:
            return
        change = update.chat_member
        if change is None or change.chat.type not in GROUP_TYPES:
            return
        if not _just_joined(change) or not self.groups.is_allowed(group_scope(change.chat.id)):
            return
        if change.new_chat_member.user.is_bot:
            return
        if not self.capabilities.deep_links:  # can't mint a start=welcome link
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
    directory: ChannelDirectory
    groups: GroupPolicy
    routes: DmRouteRegistry
    # Telegram cannot open a DM first, so a private message arrives with no
    # hint of which group prompted it and must be parked while the user picks
    # a destination. Platforms whose bot can open the DM never need this.
    pending: PendingDmMessages
    # Gates the park-and-pick handshake above: it exists only because
    # bot_can_open_dm is False here. Stated as a capability rather than
    # assumed, so the dependency is visible and a platform that gains the
    # ability skips the detour instead of inheriting it.
    capabilities: PlatformCapabilities
    welcome_text: str
    dm_page_url: str
    maintainer: str
    issues_url: str
    engine: ActionEngine
    feedback: FeedbackService | None
    bug_limiter: SlidingWindowLimiter
    token_entry: TokenEntryHandler
    subscriptions: SubscriptionHandlers | None = None

    async def on_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Deliver the welcome (R5), or redeem a configuration deep link.

        Plain ``/start`` neither mints nor resets an identity: sessions
        are created lazily by the first transcribed message, and
        rotation is the explicit ``/flush``. A ``cfg_<nonce>`` payload
        instead arms the token-entry flow for the nonce's group.
        """
        chat = self._private_chat(update)
        if chat is None:
            return
        payload = (context.args or [""])[0]
        if payload.startswith("cfg_"):
            await self.token_entry.redeem_link(update, context, dm_scope(chat.id), payload[4:])
            return
        if payload.startswith("sub_") and self.subscriptions is not None:
            await self.subscriptions.redeem_link(context, dm_scope(chat.id), payload[4:])
            return
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
        dm = dm_scope(chat.id)
        session = self.sessions.reset(dm)
        route = self.routes.route_for(dm)
        notice = self._opened_session_notice(session, route.page if route else None)
        await context.bot.send_message(chat_id=chat.id, text=f"{REPLY_FLUSHED}{notice}")

    async def on_whoami(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Disclose the current pseudonym without rotating it."""
        chat = self._private_chat(update)
        if chat is None:
            return
        dm = dm_scope(chat.id)
        session = self.sessions.peek(dm)
        if session is None:
            await context.bot.send_message(chat_id=chat.id, text=REPLY_NO_SESSION)
            return
        route = self.routes.route_for(dm)
        info = REPLY_SESSION_INFO.format(
            pseudonym=session.pseudonym.value,
            page=self.transcription.page_for(session, route.page if route else None),
        )
        await context.bot.send_message(chat_id=chat.id, text=info)

    async def on_help(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """List the private-chat commands and what transcription means."""
        chat = self._private_chat(update)
        if chat is not None:
            text = HELP_PRIVATE
            if self.maintainer:
                text += f"\n\nThis bot is maintained by {self.maintainer}"
            await context.bot.send_message(chat_id=chat.id, text=text)

    async def on_bug(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """File an anonymous bug report on the issue tracker (/bug, /issue)."""
        chat = self._private_chat(update)
        if chat is None:
            return

        async def reply(text: str) -> None:
            await context.bot.send_message(chat_id=chat.id, text=text)

        if self.feedback is None:
            await reply(REPLY_BUG_DISABLED.format(url=self.issues_url))
            return
        description = " ".join(context.args or ()).strip()
        if not description:
            await reply(REPLY_BUG_USAGE)
            return
        if not self.bug_limiter.allow("bug", str(chat.id)):
            await reply(REPLY_THROTTLED)
            return
        try:
            # Sentinel scope: a private chat id is a user identifier and
            # must never enter the pipeline — this handler routes the
            # confirmation itself.
            outcome = await self.engine.run(_BUG_SENTINEL_SCOPE, BUG_ACTION, payload=description)
        except IssueTrackerError:
            log_event("bug_report", "error")
            await reply(REPLY_BUG_FAILED.format(url=self.issues_url))
            return
        self.counters.increment("bugs_filed")
        log_event("bug_report", "ok")
        for confirmation in outcome.messages:
            await reply(confirmation.text)

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
        dm = dm_scope(chat.id)
        # An armed token entry claims the next message BEFORE anything
        # can transcribe it: a pasted secret must never reach the wiki.
        pending = self.token_entry.claims_next_message(dm)
        if pending is not None:
            # The sender id is passed explicitly rather than inferred from the
            # DM chat id: accept_token re-verifies admin-ship of the *target
            # group* against it (#27).
            sender = message.from_user
            await self.token_entry.accept_token(
                context,
                dm,
                pending,
                message.message_id,
                message.text,
                sender.id if sender is not None else None,
            )
            return
        route = self.routes.route_for(dm)
        if route is None:
            if self.capabilities.bot_can_open_dm:
                # Unreachable on Telegram: a platform whose bot opens the DM
                # knows the destination before the DM exists, so an unrouted
                # private message is simply not part of the flow.
                return
            request_id = self.pending.open_pending(dm, message.text)
            await context.bot.send_message(
                chat_id=chat.id,
                text=REPLY_DM_DESTINATION_REQUIRED,
                reply_markup=_destination_keyboard(request_id),
            )
            return
        await self._record_dm(dm, message.text, context, route.page)

    async def on_chat_shared(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Route a queued private message to a user-selected group."""
        message = update.effective_message
        chat = self._private_chat(update)
        shared = message.chat_shared if message else None
        if chat is None or shared is None:
            return
        dm = dm_scope(chat.id)
        text = self.pending.pop_pending(dm, shared.request_id)
        if text is None:
            await context.bot.send_message(
                chat_id=chat.id,
                text=REPLY_DM_DESTINATION_EXPIRED,
                reply_markup=ReplyKeyboardRemove(),
            )
            return
        group = group_scope(shared.chat_id)
        if not self.groups.is_allowed(group):
            await context.bot.send_message(
                chat_id=chat.id,
                text=REPLY_DM_DESTINATION_NOT_SERVED,
                reply_markup=ReplyKeyboardRemove(),
            )
            return
        settings = await self.directory.resolve(group)
        if (decline := self._dm_route_decline(settings)) is not None:
            await context.bot.send_message(
                chat_id=chat.id,
                text=decline,
                reply_markup=ReplyKeyboardRemove(),
            )
            return
        route = self.routes.save_route(dm, group, settings.log_page)
        await context.bot.send_message(
            chat_id=chat.id,
            text=REPLY_DM_DESTINATION_SET.format(page=route.page),
            reply_markup=ReplyKeyboardRemove(),
        )
        await self._record_dm(dm, text, context, route.page)

    async def _record_dm(
        self,
        dm: Scope,
        text: str,
        context: ContextTypes.DEFAULT_TYPE,
        target_page: str,
    ) -> None:
        chat_id = int(dm.channel)
        is_new_session = self.sessions.peek(dm) is None
        try:
            session = await self.transcription.record(dm, text, target_page=target_page)
        except WikiWriteError:
            await context.bot.send_message(chat_id=chat_id, text=REPLY_WIKI_ERROR)
            return
        self.routes.touch_route(dm)
        if is_new_session:
            # Sessions can also start (or roll over) mid-conversation;
            # tell the user which identity their words appear under.
            notice = self._opened_session_notice(session, target_page)
            await context.bot.send_message(chat_id=chat_id, text=notice)

    def _dm_route_decline(self, settings: ChannelSettings) -> str | None:
        if settings.degraded:
            return REPLY_CONFIG_UNAVAILABLE
        if self.directory.self_service_enabled and not settings.page_explicit:
            return REPLY_NO_LOG_PAGE
        return None

    def _opened_session_notice(self, session: Session, target_page: str | None = None) -> str:
        """Count and log a session opening; return the user-facing notice."""
        self.counters.increment("sessions_opened")
        log_event("session_opened", "ok")
        return REPLY_SESSION_INFO.format(
            pseudonym=session.pseudonym.value,
            page=self.transcription.page_for(session, target_page),
        )

    @staticmethod
    def _private_chat(update: Update) -> Chat | None:
        """Return the chat if this update came from a private chat."""
        chat = update.effective_chat
        if chat is None or chat.type != ChatType.PRIVATE:
            return None
        return chat


def _destination_keyboard(request_id: int) -> ReplyKeyboardMarkup:
    button = KeyboardButton(
        text=DM_DESTINATION_BUTTON,
        request_chat=KeyboardButtonRequestChat(
            request_id=request_id,
            chat_is_channel=False,
            bot_is_member=True,
            request_title=True,
        ),
    )
    return ReplyKeyboardMarkup([[button]], resize_keyboard=True, one_time_keyboard=True)
