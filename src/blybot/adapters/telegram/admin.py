"""Group self-service configuration commands (spec v2).

Admin-ship is verified **live** against Telegram on every command via
``getChatMember`` and never stored anywhere (R6). Configuration
confirmations are left visible (they document the group's choices);
only the ``/log`` flow's transient messages self-delete.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Final

from telegram.constants import ChatMemberStatus
from telegram.error import TelegramError

from blybot.adapters.telegram._common import (
    GROUP_TYPES,
    group_scope,
    scope_of,
    send_threaded,
    thread_of,
)
from blybot.domain.models import ConsentMode, Scope
from blybot.domain.ports import StorageError
from blybot.observability import Counters, log_event
from blybot.services.subscriptions import mint_subscribe_code

if TYPE_CHECKING:
    from collections.abc import Callable

    from telegram import Bot, Update
    from telegram.ext import ContextTypes

    from blybot.domain.ports import MessageArchive
    from blybot.services.binding import TokenBinding
    from blybot.services.capture import CaptureService
    from blybot.services.commands import CommandService
    from blybot.services.directory import ChannelDirectory
    from blybot.services.policy import GroupPolicy

REPLY_NOT_ADMIN: Final = "Only this group's admins can configure me."
REPLY_SELF_SERVICE_OFF: Final = (
    "Self-service configuration isn't enabled on this deployment; ask the operator."
)
REPLY_STORAGE_DOWN: Final = "Configuration is temporarily unavailable — please try again later."
# /setpage's wording now lives in the neutral CommandService (both platforms
# share it); on_setpage renders whatever CommandResult it returns.
REPLY_CONSENT_SET: Final = "Consent policy for /log is now: {mode} (group-wide)."
REPLY_CONSENT_USAGE: Final = "Usage: /setconsent immediate | author_only"
# /reset's wording now lives in the neutral CommandService (both platforms
# share it); on_reset renders whatever CommandResult it returns.
# /setrepo's binding half now lives in the neutral CommandService; only the
# deep link — Telegram's own way of collecting the secret privately — is
# appended here (Discord pops a modal instead, see issue #43).
REPLY_PAT_LINK: Final = (
    "To enable /issue and /repo here, an admin must give me a GitHub token "
    "privately — tap {link} (valid 10 minutes). Use a fine-grained PAT "
    "restricted to {repo} with Issues read/write only."
)
# /revoke's wording now lives in the neutral CommandService (both platforms
# share it); on_revoke renders whatever CommandResult it returns.
# /events on|off wording now lives in the neutral CommandService (both
# platforms share it); on_events renders whatever CommandResult it returns.
REPLY_SUBSCRIBABLE_USAGE: Final = (
    "Usage: /subscribable on | off — let members subscribe to this chat's digest in DM"
)
REPLY_SUBSCRIBABLE_ON: Final = (
    "Digest subscriptions are ON for {scope}. Share this link so members can get "
    "summaries privately: {link}\n(Digests stay empty until /capture is on here.)"
)
REPLY_SUBSCRIBABLE_OFF: Final = (
    "Digest subscriptions are OFF for {scope}. The old share link no longer works."
)
# /rule add|remove|clear and /rules wording now lives in the neutral
# CommandService too, seeded from its shared DEFAULT_RULES starter set.
# /llm show|set|reset wording now lives in the neutral CommandService (both
# platforms share it); on_llm renders whatever CommandResult it returns.
# /action add|remove|list wording now lives in the neutral CommandService
# (both platforms share it); on_action renders its CommandResult.
REPLY_CAPTURE_USAGE: Final = "Usage: /capture on | off | purge [before:YYYY-MM-DD]"
# Telegram-only affordance appended to a successful /capture toggle — Discord
# has no purge, so the shared CommandService text stays neutral (issue #32).
REPLY_CAPTURE_PURGE_HINT: Final = "Erase the archive with /capture purge."
REPLY_CAPTURE_OFF_DEPLOY: Final = (
    "Message capture isn't enabled on this deployment; ask the operator."
)
# /capture on|off wording now lives in the neutral CommandService; only the
# purge branch, which is Telegram-only in this increment, keeps its reply here.
REPLY_CAPTURE_PURGED: Final = "Archive erased for {scope}: {count} message(s) deleted."
SETUP_TEXT: Final = (
    "I'm configurable by this group's admins, right here:\n\n"
    "(run a command IN a topic to set that topic; in General for the group)\n\n"
    "/setpage <page path> — where /log publishes (under <path>/{suffix})\n"
    "/setconsent immediate|author_only — who may /log whose messages\n"
    "/setrepo owner/repo — bind a GitHub repo (then /issue, /repo)\n"
    "/events on|off — turn rule-driven repo notifications on/off\n"
    "/rule add|remove|clear — composable event rules (see /rules)\n"
    "/rules — list this chat's event rules\n"
    "/capture on|off|purge — archive this chat's messages for analyses\n"
    "/summarize · /talkingpoints · /stats [24h|7d] — publish an analysis now\n"
    "/action add|remove|list — schedule recurring analyses\n"
    "/llm show|set|reset — this chat's model, output language, parameters\n"
    "/revoke — discard this group's stored GitHub token\n"
    "/settings — current configuration\n"
    "/reset — forget everything and return to defaults\n\n"
    "Everything I publish is public and permanent; see /settings for "
    "where it currently lands."
)
# /settings' wording now lives in the neutral CommandService (both platforms
# share it); on_settings renders whatever CommandResult it returns.

_ADMIN_STATUSES: Final = frozenset({ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.OWNER})


async def is_group_admin(bot: Bot, chat_id: int, user_id: int) -> bool:
    """Whether the user administers the chat — checked live, never stored."""
    try:
        member = await bot.get_chat_member(chat_id=chat_id, user_id=user_id)
    except TelegramError:
        return False
    return member.status in _ADMIN_STATUSES


def _scope(scope: Scope) -> str:
    return "this topic" if scope.thread else "the group default"


def _target(scope: Scope) -> tuple[int, int]:
    """The scope's Telegram ``(chat_id, thread_id)`` for topic-routed replies."""
    return int(scope.channel), int(scope.thread) if scope.thread else 0


# Sentinel distinguishing "no before: given" (None) from "unparsable".
_BAD_DATE: Final = datetime(1, 1, 1, tzinfo=UTC)


def _parse_before(token: str) -> datetime | None:
    """Parse an optional ``before:YYYY-MM-DD`` purge bound (UTC midnight)."""
    if not token:
        return None
    key, sep, value = token.partition(":")
    if key != "before" or not sep:
        return _BAD_DATE
    try:
        return datetime.strptime(value, "%Y-%m-%d").replace(tzinfo=UTC)
    except ValueError:
        return _BAD_DATE


@dataclass(eq=False)
class AdminHandlers:
    """Handlers for the group configuration commands."""

    directory: ChannelDirectory
    groups: GroupPolicy
    counters: Counters
    page_url_for: Callable[[str], str]
    binding: TokenBinding
    # The neutral service behind every shared command. It also carries the
    # vault and repo gateway the /setrepo flow needs, so this handler holds
    # neither itself.
    commands: CommandService
    # Capture-enabled deployments only: the archive behind /capture purge
    # and the ingest service whose policy cache /capture must invalidate.
    archive: MessageArchive | None = None
    capture_service: CaptureService | None = None
    # /action's store and clock now live on the shared CommandService too,
    # alongside /llm's defaults — this handler holds neither.

    async def on_setup(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Explain the self-service commands to an admin."""
        scope = await self._admin_chat(update, context)
        if scope is not None:
            chat_id, thread_id = _target(scope)
            text = SETUP_TEXT.format(suffix=self.directory.page_suffix or "<disabled>")
            await self._reply(context, chat_id, thread_id, text)

    async def on_setpage(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Point this group's /log at a page under the allowed prefix.

        The admin gate stays here (it produces silence for unlisted/v1 chats
        that the neutral service can't); once it confirms an admin, the
        page-resolution logic and wording are the shared CommandService's.
        """
        scope = await self._admin_chat(update, context)
        if scope is None:
            return
        chat_id, thread_id = _target(scope)
        page = " ".join(context.args or ())
        result = await self.commands.set_page(scope, is_admin=True, page=page)
        await self._reply(context, chat_id, thread_id, result.text)

    async def on_setconsent(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Set this group's consent policy for /log."""
        scope = await self._admin_chat(update, context)
        if scope is None:
            return
        chat_id, thread_id = _target(scope)
        argument = (context.args or [""])[0]
        if argument not in {ConsentMode.IMMEDIATE.value, ConsentMode.AUTHOR_ONLY.value}:
            await self._reply(context, chat_id, thread_id, REPLY_CONSENT_USAGE)
            return
        try:
            await self.directory.set_consent(scope, ConsentMode(argument))
        except StorageError:
            await self._reply(context, chat_id, thread_id, REPLY_STORAGE_DOWN)
            return
        log_event("profile_update", "ok")
        await self._reply(context, chat_id, thread_id, REPLY_CONSENT_SET.format(mode=argument))

    async def on_setrepo(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Bind this group to a GitHub repository and start the token flow."""
        scope = await self._admin_chat(update, context)
        if scope is None:
            return
        chat_id, thread_id = _target(scope)
        repo = ((context.args or [""])[0]).strip()
        result = await self.commands.set_repo(scope, is_admin=True, repo=repo)
        if not result.ok:
            await self._reply(context, chat_id, thread_id, result.text)
            return
        # Telegram collects the token over a deep link into DM; the link is
        # this platform's affordance, so it is appended to the neutral
        # confirmation rather than living in the shared service.
        nonce = self.binding.mint_link(scope)
        link = f"https://t.me/{context.bot.username}?start=cfg_{nonce}"
        await self._reply(
            context,
            chat_id,
            thread_id,
            f"{result.text} {REPLY_PAT_LINK.format(repo=repo, link=link)}",
        )

    async def on_subscribable(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Toggle whether members may subscribe to this scope's digest (§21)."""
        scope = await self._admin_chat(update, context)
        if scope is None:
            return
        chat_id, thread_id = _target(scope)
        argument = ((context.args or [""])[0]).lower()
        if argument not in {"on", "off"}:
            await self._reply(context, chat_id, thread_id, REPLY_SUBSCRIBABLE_USAGE)
            return
        try:
            if argument == "on":
                code = mint_subscribe_code()
                await self.directory.set_subscribe_code(scope, code)
                link = f"https://t.me/{context.bot.username}?start=sub_{code}"
                reply = REPLY_SUBSCRIBABLE_ON.format(scope=_scope(scope), link=link)
            else:
                await self.directory.set_subscribe_code(scope, None)
                reply = REPLY_SUBSCRIBABLE_OFF.format(scope=_scope(scope))
            log_event("profile_update", "ok")
        except StorageError:
            reply = REPLY_STORAGE_DOWN
        await self._reply(context, chat_id, thread_id, reply)

    async def on_revoke(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Discard this group's stored API token (delegates to the shared service)."""
        scope = await self._admin_chat(update, context)
        if scope is None:
            return
        chat_id, thread_id = _target(scope)
        result = await self.commands.revoke_token(scope, is_admin=True)
        await self._reply(context, chat_id, thread_id, result.text)

    async def on_events(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Switch this (group, topic)'s rule-driven notifications on or off."""
        scope = await self._admin_chat(update, context)
        if scope is None:
            return
        chat_id, thread_id = _target(scope)
        result = await self.commands.events(scope, is_admin=True, tokens=list(context.args or ()))
        await self._reply(context, chat_id, thread_id, result.text)

    async def on_capture(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Switch this scope's message capture on/off, or erase its archive.

        ``on``/``off`` are the shared CommandService's; ``purge`` stays here —
        it is Telegram-only in this increment (Discord exposes no purge).
        """
        scope = await self._admin_chat(update, context)
        if scope is None:
            return
        chat_id, thread_id = _target(scope)
        args = list(context.args or [""])
        argument = args[0].lower() if args else ""
        before = _parse_before(args[1] if len(args) > 1 else "")
        if argument not in {"on", "off", "purge"} or before is _BAD_DATE:
            await self._reply(context, chat_id, thread_id, REPLY_CAPTURE_USAGE)
            return
        if argument == "purge":
            reply = await self._purge_capture(scope, before)
        else:
            result = await self.commands.capture(scope, is_admin=True, enabled=argument == "on")
            # Re-append Telegram's own erase hint on a successful toggle; the
            # shared service text is neutral because Discord has no purge.
            reply = f"{result.text} {REPLY_CAPTURE_PURGE_HINT}" if result.ok else result.text
        await self._reply(context, chat_id, thread_id, reply)

    async def _purge_capture(self, scope: Scope, before: datetime | None) -> str:
        """Erase this scope's archive (older than ``before`` if given)."""
        archive, service = self.archive, self.capture_service
        if archive is None or service is None:
            return REPLY_CAPTURE_OFF_DEPLOY
        try:
            count = await archive.purge(scope, before)
        except StorageError:
            return REPLY_STORAGE_DOWN
        log_event("capture_purge", "ok")
        return REPLY_CAPTURE_PURGED.format(scope=_scope(scope), count=count)

    async def on_action(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Add, remove, or list this scope's scheduled actions (v3 phase 4)."""
        scope = await self._admin_chat(update, context)
        if scope is None:
            return
        chat_id, thread_id = _target(scope)
        result = await self.commands.action(scope, is_admin=True, tokens=list(context.args or ()))
        await self._reply(context, chat_id, thread_id, result.text)

    async def on_llm(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Show, set, or reset this scope's LLM settings (v3 §2.4).

        The admin gate stays here; the grammar, validation, and wording are
        the shared CommandService's — this only maps the trigger.
        """
        scope = await self._admin_chat(update, context)
        if scope is None:
            return
        chat_id, thread_id = _target(scope)
        result = await self.commands.set_llm(scope, is_admin=True, tokens=list(context.args or ()))
        await self._reply(context, chat_id, thread_id, result.text)

    async def on_bridge(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Create, join, leave or show this scope's cross-platform bridge (#81)."""
        scope = await self._admin_chat(update, context)
        if scope is None:
            return
        chat_id, thread_id = _target(scope)
        result = await self.commands.bridge(scope, is_admin=True, tokens=list(context.args or ()))
        await self._reply(context, chat_id, thread_id, result.text)

    async def on_rule(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Add, remove, or clear this (group, topic)'s composable event rules."""
        scope = await self._admin_chat(update, context)
        if scope is None:
            return
        chat_id, thread_id = _target(scope)
        result = await self.commands.rule(scope, is_admin=True, tokens=list(context.args or ()))
        await self._reply(context, chat_id, thread_id, result.text)

    async def on_rules(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """List this (group, topic)'s event rules with their ids."""
        scope = await self._admin_chat(update, context)
        if scope is None:
            return
        chat_id, thread_id = _target(scope)
        result = await self.commands.list_rules(scope, is_admin=True)
        await self._reply(context, chat_id, thread_id, result.text)

    async def on_settings(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Show this group's effective configuration.

        The admin gate stays here; the resolution and formatting are the
        shared CommandService's — this only maps the trigger.
        """
        scope = await self._admin_chat(update, context)
        if scope is None:
            return
        chat_id, thread_id = _target(scope)
        result = await self.commands.show_settings(scope, is_admin=True)
        await self._reply(context, chat_id, thread_id, result.text)

    async def on_reset(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Forget this group's profile entirely (delegates to the shared service)."""
        scope = await self._admin_chat(update, context)
        if scope is None:
            return
        chat_id, thread_id = _target(scope)
        result = await self.commands.reset(scope, is_admin=True)
        await self._reply(context, chat_id, thread_id, result.text)

    @staticmethod
    async def _reply(
        context: ContextTypes.DEFAULT_TYPE, chat_id: int, thread_id: int, text: str
    ) -> None:
        await send_threaded(context.bot, chat_id, thread_id, text)

    async def _admin_chat(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> Scope | None:
        """Return the addressed :class:`Scope` when the sender is a group admin."""
        chat = update.effective_chat
        message = update.effective_message
        if chat is None or message is None or chat.type not in GROUP_TYPES:
            return None
        if not self.groups.is_allowed(group_scope(chat.id)):
            return None  # unlisted groups get silence, same as /log
        if not self.directory.self_service_enabled:
            # v1 deployment: stay silent (and skip the getChatMember
            # round-trip) — group /help doesn't advertise these commands.
            log_event("admin_command", "ignored")
            return None
        thread_id = thread_of(update)
        user = message.from_user
        if user is None or not await is_group_admin(context.bot, chat.id, user.id):
            await self._reply(context, chat.id, thread_id, REPLY_NOT_ADMIN)
            return None
        return scope_of(update)
