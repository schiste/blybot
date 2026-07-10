"""Group self-service configuration commands (spec v2).

Admin-ship is verified **live** against Telegram on every command via
``getChatMember`` and never stored anywhere (R6). Configuration
confirmations are left visible (they document the group's choices);
only the ``/log`` flow's transient messages self-delete.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Final

from telegram.constants import ChatMemberStatus, ChatType
from telegram.error import TelegramError

from blybot.domain.models import ConsentMode
from blybot.domain.ports import StorageError
from blybot.observability import Counters, log_event
from blybot.services.directory import PageNotAllowedError, SelfServiceUnavailableError

if TYPE_CHECKING:
    from collections.abc import Callable

    from telegram import Bot, Chat, Update
    from telegram.ext import ContextTypes

    from blybot.services.directory import ChannelDirectory

REPLY_NOT_ADMIN: Final = "Only this group's admins can configure me."
REPLY_SELF_SERVICE_OFF: Final = (
    "Self-service configuration isn't enabled on this deployment; ask the operator."
)
REPLY_STORAGE_DOWN: Final = "Configuration is temporarily unavailable — please try again later."
REPLY_PAGE_SET: Final = "Done. This group's /log now publishes to {url}"
REPLY_PAGE_REFUSED: Final = (
    'That page isn\'t allowed: pick a subpage of "{prefix}" — e.g. /setpage {prefix}YourGroup'
)
REPLY_SETPAGE_USAGE: Final = "Usage: /setpage {prefix}YourGroup"
REPLY_CONSENT_SET: Final = "Consent policy for /log is now: {mode}"
REPLY_CONSENT_USAGE: Final = "Usage: /setconsent immediate | author_only"
REPLY_RESET: Final = "Forgotten. This group is back on the operator defaults."
SETUP_TEXT: Final = (
    "I'm configurable by this group's admins, right here:\n\n"
    "/setpage {prefix}YourGroup — where /log publishes\n"
    "/setconsent immediate|author_only — who may /log whose messages\n"
    "/settings — current configuration\n"
    "/reset — forget everything and return to defaults\n\n"
    "Everything I publish is public and permanent; see /settings for "
    "where it currently lands."
)
SETTINGS_TEMPLATE: Final = (
    "Current configuration{customized}:\n"
    "- /log publishes to: {log_page}\n"
    "- consent policy: {consent}\n"
    "- GitHub repo: {repo}\n"
    "- repo token stored: {token}\n"
    "- repo notifications: {events}"
)

_ADMIN_STATUSES: Final = frozenset({ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.OWNER})
_GROUP_TYPES: Final = frozenset({ChatType.GROUP, ChatType.SUPERGROUP})


async def is_group_admin(bot: Bot, chat_id: int, user_id: int) -> bool:
    """Whether the user administers the chat — checked live, never stored."""
    try:
        member = await bot.get_chat_member(chat_id=chat_id, user_id=user_id)
    except TelegramError:
        return False
    return member.status in _ADMIN_STATUSES


@dataclass(eq=False)
class AdminHandlers:
    """Handlers for the group configuration commands."""

    directory: ChannelDirectory
    counters: Counters
    page_url_for: Callable[[str], str]

    async def on_setup(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Explain the self-service commands to an admin."""
        chat = await self._admin_chat(update, context)
        if chat is not None:
            text = SETUP_TEXT.format(prefix=self.directory.page_prefix or "<disabled>")
            await context.bot.send_message(chat_id=chat.id, text=text)

    async def on_setpage(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Point this group's /log at a page under the allowed prefix."""
        chat = await self._admin_chat(update, context)
        if chat is None:
            return
        title = " ".join(context.args or ()).strip()
        if not title:
            usage = REPLY_SETPAGE_USAGE.format(prefix=self.directory.page_prefix)
            await context.bot.send_message(chat_id=chat.id, text=usage)
            return
        try:
            normalized = await self.directory.set_log_page(chat.id, title)
        except PageNotAllowedError:
            refused = REPLY_PAGE_REFUSED.format(prefix=self.directory.page_prefix)
            await context.bot.send_message(chat_id=chat.id, text=refused)
            return
        except SelfServiceUnavailableError:
            await context.bot.send_message(chat_id=chat.id, text=REPLY_SELF_SERVICE_OFF)
            return
        except StorageError:
            await context.bot.send_message(chat_id=chat.id, text=REPLY_STORAGE_DOWN)
            return
        self.counters.increment("profiles_configured")
        log_event("profile_update", "ok")
        confirmation = REPLY_PAGE_SET.format(url=self.page_url_for(normalized))
        await context.bot.send_message(chat_id=chat.id, text=confirmation)

    async def on_setconsent(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Set this group's consent policy for /log."""
        chat = await self._admin_chat(update, context)
        if chat is None:
            return
        argument = (context.args or [""])[0]
        if argument not in {ConsentMode.IMMEDIATE.value, ConsentMode.AUTHOR_ONLY.value}:
            await context.bot.send_message(chat_id=chat.id, text=REPLY_CONSENT_USAGE)
            return
        try:
            await self.directory.set_consent(chat.id, ConsentMode(argument))
        except StorageError:
            await context.bot.send_message(chat_id=chat.id, text=REPLY_STORAGE_DOWN)
            return
        log_event("profile_update", "ok")
        await context.bot.send_message(
            chat_id=chat.id, text=REPLY_CONSENT_SET.format(mode=argument)
        )

    async def on_settings(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Show this group's effective configuration."""
        chat = await self._admin_chat(update, context)
        if chat is None:
            return
        settings = await self.directory.resolve(chat.id)
        text = SETTINGS_TEMPLATE.format(
            customized="" if settings.customized else " (all defaults)",
            log_page=self.page_url_for(settings.log_page),
            consent=settings.consent_mode.value,
            repo=settings.repo or "none",
            token="yes" if settings.has_token else "no",
            events="on" if settings.events_enabled else "off",
        )
        await context.bot.send_message(chat_id=chat.id, text=text)

    async def on_reset(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Forget this group's profile entirely."""
        chat = await self._admin_chat(update, context)
        if chat is None:
            return
        try:
            await self.directory.reset(chat.id)
        except StorageError:
            await context.bot.send_message(chat_id=chat.id, text=REPLY_STORAGE_DOWN)
            return
        log_event("profile_reset", "ok")
        await context.bot.send_message(chat_id=chat.id, text=REPLY_RESET)

    async def _admin_chat(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> Chat | None:
        """Return the group chat when the sender is one of its admins."""
        chat = update.effective_chat
        message = update.effective_message
        if chat is None or message is None or chat.type not in _GROUP_TYPES:
            return None
        user = message.from_user
        if user is None or not await is_group_admin(context.bot, chat.id, user.id):
            await context.bot.send_message(chat_id=chat.id, text=REPLY_NOT_ADMIN)
            return None
        if not self.directory.self_service_enabled:
            await context.bot.send_message(chat_id=chat.id, text=REPLY_SELF_SERVICE_OFF)
            return None
        return chat
