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

from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ChatMemberStatus, ChatType
from telegram.error import TelegramError

from blybot.adapters.telegram.admin import is_group_admin
from blybot.domain.models import ConsentMode
from blybot.domain.ports import IssueTrackerError, StorageError, WikiWriteError
from blybot.observability import Counters, log_event
from blybot.services.publish import NothingToPublishError
from blybot.services.repo import NoRepoBoundError, NoTokenError

if TYPE_CHECKING:
    from collections.abc import Callable

    from telegram import Bot, Chat, ChatMemberUpdated, Message, Update
    from telegram.ext import ContextTypes

    from blybot.domain.models import Session
    from blybot.domain.ports import RepoActions, TokenVault
    from blybot.services.binding import TokenBinding
    from blybot.services.directory import ChannelDirectory
    from blybot.services.feedback import FeedbackService
    from blybot.services.policy import GroupPolicy, SlidingWindowLimiter
    from blybot.services.publish import LogPublicationService
    from blybot.services.repo import GroupRepoService
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
REPLY_PUBLISHED: Final = "Published anonymously: {url}"
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
REPLY_BUG_USAGE: Final = (
    "Describe the problem after the command, e.g.: /bug the bot ignored my /flush"
)
REPLY_BUG_FILED: Final = "Filed anonymously: {url}"
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
REPLY_ISSUE_USAGE: Final = "Describe the issue after the command: /issue something is broken"
REPLY_ISSUE_UNBOUND: Final = (
    "No repository is bound to this group — an admin can bind one with /setrepo."
)
REPLY_ISSUE_DISABLED: Final = "Repository features aren't enabled on this deployment."
REPLY_ISSUE_NO_PAT: Final = (
    "A repository is bound but its token step was never completed — an admin "
    "should run /setrepo again and follow the private link."
)
REPLY_ISSUE_FAILED: Final = "Sorry, GitHub refused that — the token may have expired (/setrepo)."
REPLY_REPO_SUMMARY: Final = (
    "{repo}: {count} open items. Recent: {titles}\nhttps://github.com/{repo}/issues"
)
REPLY_LINK_EXPIRED: Final = (
    "That configuration link is no longer valid. Run /setrepo in the group again for a fresh one."
)
REPLY_LINK_NOT_ADMIN: Final = "Only an admin of that group can supply its token."
REPLY_PAT_PROMPT: Final = (
    "Paste the GitHub token for {repo} as your next message here. Use a "
    "fine-grained PAT restricted to that repository with Issues read/write "
    "only. I'll validate it, encrypt it, store it — and delete your message "
    "from this chat immediately. This prompt expires in 5 minutes; while "
    "it's active, nothing you send me is transcribed."
)
REPLY_PAT_NO_REPO: Final = "That group no longer has a repository bound; run /setrepo there first."
REPLY_PAT_INVALID: Final = (
    "GitHub rejected that token for the bound repository — check the repo "
    "access and Issues permission, then paste it again."
)
REPLY_PAT_STORE_FAILED: Final = (
    "Storing the token failed on my side — please paste it again in a moment."
)
REPLY_PAT_SAVED: Final = (
    "Token validated, encrypted and stored; I've deleted your message. "
    "/issue and /repo are live in the group; /revoke there discards the token."
)
NEWCOMER_PROMPT: Final = "Welcome! Tap below for a private note on how I work."
NEWCOMER_BUTTON: Final = "What is this bot?"
HELP_PRIVATE: Final = (
    "Anything you write to me here is transcribed to a public Meta-wiki page "
    "under a random per-session pseudonym.\n\n"
    "/whoami — show the pseudonym you currently appear as\n"
    "/flush — discard it and mint a fresh, unlinkable one\n"
    "/privacy — what I collect and publish\n"
    "/bug — file an anonymous bug report with my maintainer\n"
    "/help — this message\n\n"
    "In groups, reply to a message with /log to publish it anonymously."
)
HELP_GROUP: Final = (
    "Reply to a message with /log to publish it anonymously to the Meta-wiki "
    "log. Message me privately for anonymous transcription — /help there for "
    "details."
)
HELP_GROUP_SELF_SERVICE: Final = (
    " /issue files a bug in this group's repo; /repo shows its status. "
    "Group admins: /setup to configure me."
)
PRIVACY_TEXT: Final = (
    "What I ingest: only messages explicitly marked with /log in groups, and "
    "what you send me in this private chat. Telegram's privacy mode means "
    "ordinary group chatter is never even delivered to me.\n\n"
    "What I publish: sanitized text on public Meta-wiki pages — permanently. "
    "/log entries carry no attribution at all; private messages appear under "
    "a random per-session pseudonym that is never derived from your account.\n\n"
    "What I store: no user IDs, usernames, or message archives — ever. "
    "Sessions live only in memory and vanish on timeout, /flush, or restart. "
    "For groups whose admins configure me, I keep that group's chat id, its "
    "chosen page/repository, and (encrypted) any API token an admin "
    "supplies — /reset in the group deletes all of it. My operational logs "
    "contain no content and no identifiers.\n\n"
    "What I cannot protect: content that identifies you in its own words, "
    "and the wiki's public edit timestamps.\n\n"
    "How I run: as a continuous job on Wikimedia Toolforge (Kubernetes), "
    "movement-hosted infrastructure — no third-party servers or analytics. "
    "Credentials live in a permission-restricted file on the tool account; "
    "there is no database. I am free software (AGPL-3.0): every line, "
    "including this message, is auditable at https://github.com/schiste/blybot"
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


def _thread_of(update: Update) -> int:
    """The forum topic the message was sent in; 0 for General/non-forum.

    Gated on ``is_topic_message`` so a reply chain in a *non-forum*
    supergroup (which also populates ``message_thread_id``) is not
    mistaken for a topic.
    """
    message = update.effective_message
    if message is None or not message.is_topic_message:
        return 0
    return message.message_thread_id or 0


def _repo_error_reply(error: Exception) -> str:
    """One place mapping repo-service failures to user-facing replies."""
    if isinstance(error, NoRepoBoundError):
        return REPLY_ISSUE_UNBOUND
    if isinstance(error, NoTokenError):
        return REPLY_ISSUE_NO_PAT
    if isinstance(error, IssueTrackerError):
        log_event("group_issue", "error")
    return REPLY_ISSUE_FAILED


def _help_footer(page_url: str, maintainer: str) -> str:
    """Publication link and maintainer mention appended to both /help texts."""
    footer = f"\n\nEverything I publish lands at {page_url}"
    if maintainer:
        footer += f"\nThis bot is maintained by {maintainer}"
    return footer


@dataclass(eq=False)
class GroupHandlers:
    """Handlers for the group ``/log`` flow, greeting, and migration."""

    log_service: LogPublicationService
    groups: GroupPolicy
    limiter: SlidingWindowLimiter
    directory: ChannelDirectory
    page_url_for: Callable[[str], str]
    counters: Counters
    group_greeting_text: str
    maintainer: str
    newcomer_welcome_enabled: bool
    repo_service: GroupRepoService | None
    # The /log command message is deleted after this delay, hiding who
    # requested the publication. Requires the "Delete messages" admin
    # right; without it the cleanup is skipped silently.
    cleanup_delay_seconds: float = 5.0
    # The bot's own /log replies (confirmation, hints) self-delete after
    # this delay — long enough to read, then the group stays tidy.
    # Deleting its own messages needs no admin right.
    reply_cleanup_delay_seconds: float = 15.0
    _cleanup_tasks: set[asyncio.Task[None]] = field(default_factory=set, init=False)

    async def on_log(  # noqa: PLR0911 -- one early return per decline reason
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
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
        await self._schedule_cleanup(
            context.bot, chat.id, message.message_id, self.cleanup_delay_seconds
        )
        thread_id = _thread_of(update)

        async def reply(text: str) -> None:
            sent = await context.bot.send_message(
                chat_id=chat.id, text=text, message_thread_id=thread_id or None
            )
            await self._schedule_cleanup(
                context.bot, chat.id, sent.message_id, self.reply_cleanup_delay_seconds
            )

        target = message.reply_to_message
        if target is None:
            await reply(REPLY_USAGE)
            return
        settings = await self.directory.resolve(chat.id, thread_id)
        if settings.degraded:
            # Fail closed: the group's consent policy and target page are
            # unknown right now; publishing on defaults could violate both.
            await reply(REPLY_CONFIG_UNAVAILABLE)
            return
        if self.directory.self_service_enabled and not settings.page_explicit:
            # A self-service group must choose its own page — never leak
            # its logs onto the shared operator default.
            await reply(REPLY_NO_LOG_PAGE)
            return
        if settings.consent_mode is ConsentMode.AUTHOR_ONLY and not _same_author(message, target):
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
            heading = await self.log_service.publish(target.text, target_page=settings.log_page)
        except NothingToPublishError:
            self.counters.increment("log_declined_media")
            await reply(REPLY_MEDIA_DECLINED)
        except WikiWriteError:
            await reply(REPLY_WIKI_ERROR)
        else:
            log_event("log_command", "ok")
            page_url = self.page_url_for(settings.log_page)
            section_url = f"{page_url}#{heading.replace(' ', '_')}"
            await reply(REPLY_PUBLISHED.format(url=section_url))

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
        try:
            await self.directory.migrate(message.chat.id, message.migrate_to_chat_id)
        except StorageError:
            log_event("chat_migration", "error")
            return
        log_event("chat_migration", "ok" if applied else "ignored")

    def _within_rate_limits(self, message: Message, chat_id: int) -> bool:
        if not self.limiter.allow("group", chat_id):
            return False
        user = message.from_user
        return user is None or self.limiter.allow("user", user.id)

    async def on_issue(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """File an anonymous issue in the group's bound repository."""
        chat = self._served_group(update)
        if chat is None:
            return
        thread_id = _thread_of(update)

        async def reply(text: str) -> None:
            await context.bot.send_message(
                chat_id=chat.id, text=text, message_thread_id=thread_id or None
            )

        description = " ".join(context.args or ()).strip()
        if not description:
            await reply(REPLY_ISSUE_USAGE)
            return
        if self.repo_service is None:
            await reply(REPLY_ISSUE_DISABLED)
            return
        if not self.limiter.allow("issue", chat.id):
            await reply(REPLY_THROTTLED)
            return
        try:
            url = await self.repo_service.file_issue(chat.id, thread_id, description)
        except (NoRepoBoundError, NoTokenError, StorageError, IssueTrackerError) as error:
            await reply(_repo_error_reply(error))
        else:
            self.counters.increment("group_issues_filed")
            log_event("group_issue", "ok")
            await reply(REPLY_BUG_FILED.format(url=url))

    async def on_repo(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Show the bound repository's open-items summary."""
        chat = self._served_group(update)
        if chat is None:
            return
        thread_id = _thread_of(update)

        async def reply(text: str) -> None:
            await context.bot.send_message(
                chat_id=chat.id, text=text, message_thread_id=thread_id or None
            )

        if self.repo_service is None:
            await reply(REPLY_ISSUE_DISABLED)
            return
        if not self.limiter.allow("repo", chat.id):
            await reply(REPLY_THROTTLED)
            return
        try:
            summary = await self.repo_service.summary(chat.id, thread_id)
        except (NoRepoBoundError, NoTokenError, StorageError, IssueTrackerError) as error:
            await reply(_repo_error_reply(error))
        else:
            await reply(
                REPLY_REPO_SUMMARY.format(
                    repo=summary.repo,
                    count=summary.open_count,
                    titles="; ".join(summary.recent_titles) or "none",
                )
            )

    def _served_group(self, update: Update) -> Chat | None:
        """Return the chat when this is a group the bot serves."""
        chat = update.effective_chat
        if chat is None or chat.type not in _GROUP_TYPES or not self.groups.is_allowed(chat.id):
            return None
        return chat

    async def on_help(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Explain the /log gesture when asked in a served group."""
        chat = update.effective_chat
        if chat is None or chat.type not in _GROUP_TYPES or not self.groups.is_allowed(chat.id):
            return
        thread_id = _thread_of(update)
        settings = await self.directory.resolve(chat.id, thread_id)
        page_url = self.page_url_for(settings.log_page)
        text = HELP_GROUP
        if self.directory.self_service_enabled:
            text += HELP_GROUP_SELF_SERVICE
        text += _help_footer(page_url, self.maintainer)
        await context.bot.send_message(
            chat_id=chat.id, text=text, message_thread_id=thread_id or None
        )

    async def _schedule_cleanup(
        self, bot: Bot, chat_id: int, message_id: int, delay_seconds: float
    ) -> None:
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
    dm_page_url: str
    maintainer: str
    issues_url: str
    feedback: FeedbackService | None
    bug_limiter: SlidingWindowLimiter
    binding: TokenBinding
    directory: ChannelDirectory
    gateway: RepoActions | None
    vault: TokenVault | None

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
            await self._redeem_configuration_link(update, context, chat.id, payload[4:])
            return
        await context.bot.send_message(chat_id=chat.id, text=self.welcome_text)
        log_event("welcome_delivered", "ok")

    async def _redeem_configuration_link(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE, dm_chat_id: int, nonce: str
    ) -> None:
        async def reply(text: str) -> None:
            await context.bot.send_message(chat_id=dm_chat_id, text=text)

        target = self.binding.peek_link(nonce)
        message = update.effective_message
        user = message.from_user if message else None
        if target is None:
            await reply(REPLY_LINK_EXPIRED)
            return
        group_chat_id, thread_id = target
        if user is None or not await is_group_admin(context.bot, group_chat_id, user.id):
            # Deliberately NOT consumed: a non-admin tapping the public
            # link must not burn it for the real admin.
            await reply(REPLY_LINK_NOT_ADMIN)
            return
        if self.binding.redeem_link(nonce) is None:  # consumed in a race
            await reply(REPLY_LINK_EXPIRED)
            return
        settings = await self.directory.resolve(group_chat_id, thread_id)
        if not settings.repo:
            await reply(REPLY_PAT_NO_REPO)
            return
        self.binding.open_entry(dm_chat_id, group_chat_id, thread_id)
        log_event("token_entry_opened", "ok")
        await reply(REPLY_PAT_PROMPT.format(repo=settings.repo))

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
            text = HELP_PRIVATE + _help_footer(self.dm_page_url, self.maintainer)
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
        if not self.bug_limiter.allow("bug", chat.id):
            await reply(REPLY_THROTTLED)
            return
        try:
            url = await self.feedback.report(description)
        except IssueTrackerError:
            log_event("bug_report", "error")
            await reply(REPLY_BUG_FAILED.format(url=self.issues_url))
            return
        self.counters.increment("bugs_filed")
        log_event("bug_report", "ok")
        await reply(REPLY_BUG_FILED.format(url=url))

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
        # An armed token entry claims the next message BEFORE anything
        # can transcribe it: a pasted secret must never reach the wiki.
        pending = self.binding.pending_target(chat.id)
        if pending is not None:
            # Remove the pasted secret from the chat first — bots may
            # delete messages in private chats, so don't rely on the
            # admin remembering to.
            try:
                await context.bot.delete_message(chat_id=chat.id, message_id=message.message_id)
            except TelegramError:
                log_event("command_cleanup", "ignored")
            await self._accept_token(context, chat.id, pending, message.text)
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

    async def _accept_token(
        self,
        context: ContextTypes.DEFAULT_TYPE,
        dm_chat_id: int,
        target: tuple[int, int],
        text: str,
    ) -> None:
        group_chat_id, thread_id = target

        async def reply(reply_text: str) -> None:
            await context.bot.send_message(chat_id=dm_chat_id, text=reply_text)

        if self.gateway is None or self.vault is None:
            self.binding.close_entry(dm_chat_id)
            await reply(REPLY_PAT_NO_REPO)
            return
        settings = await self.directory.resolve(group_chat_id, thread_id)
        if not settings.repo:
            self.binding.close_entry(dm_chat_id)
            await reply(REPLY_PAT_NO_REPO)
            return
        token = text.strip()
        if not await self.gateway.validate_token(settings.repo, token):
            await reply(REPLY_PAT_INVALID)  # entry stays armed for a retry
            return
        try:
            await self.vault.store_token(group_chat_id, thread_id, token)
        except StorageError:
            await reply(REPLY_PAT_STORE_FAILED)  # entry stays armed
            return
        self.binding.close_entry(dm_chat_id)
        self.counters.increment("tokens_bound")
        log_event("token_bound", "ok")
        await reply(REPLY_PAT_SAVED)

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
