"""Group self-service configuration commands (spec v2).

Admin-ship is verified **live** against Telegram on every command via
``getChatMember`` and never stored anywhere (R6). Configuration
confirmations are left visible (they document the group's choices);
only the ``/log`` flow's transient messages self-delete.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, replace
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
from blybot.domain.models import ConsentMode, LlmSettings, Scope
from blybot.domain.ports import StorageError
from blybot.observability import Counters, log_event
from blybot.services.actions import (
    MAX_ACTIONS,
    ActionParseError,
    describe_action,
    parse_action,
)
from blybot.services.directory import TooManyRulesError
from blybot.services.llmconf import LlmParseError, describe_llm, parse_llm_args
from blybot.services.rules import MAX_RULES, RuleParseError, describe_rule, parse_rule
from blybot.services.subscriptions import mint_subscribe_code

if TYPE_CHECKING:
    from collections.abc import Callable

    from telegram import Bot, Update
    from telegram.ext import ContextTypes

    from blybot.domain.ports import ActionStore, Clock, MessageArchive, TokenVault
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
REPLY_RESET: Final = "Forgotten. {scope} is back on the inherited defaults."
REPLY_SETREPO_USAGE: Final = "Usage: /setrepo owner/repository"
REPLY_REPO_BOUND: Final = (
    "Repo bound for {scope}: {repo} (any previously stored token was "
    "discarded). To enable /issue and /repo here, an admin must give me a "
    "GitHub token privately — tap {link} (valid 10 minutes). Use a "
    "fine-grained PAT restricted to {repo} with Issues read/write only."
)
REPLY_PAT_REVOKED: Final = "Token discarded. /issue and /repo are disabled for this group."
REPLY_EVENTS_NEED_REPO: Final = (
    "Bind a repository at this scope first: /setrepo owner/repo here in the "
    "topic (or in General for the group), then /events on."
)
REPLY_EVENTS_USAGE: Final = "Usage: /events on | off — rules decide what's delivered (see /rules)"
REPLY_EVENTS_SET: Final = "Repo notifications: {state}."
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
REPLY_EVENTS_SEEDED: Final = (
    "Repo notifications on. Seeded two starter rules — pr.merged and release, "
    "both digest. Use /rules to see them, /rule to add more, /rule clear to start over."
)
# Sensible defaults so /events on works out of the box; admins refine them.
DEFAULT_RULES: Final = ("pr.merged digest", "release digest")
REPLY_RULE_USAGE: Final = (
    "Usage:\n"
    "/rule add <event> [filters] [live|digest]\n"
    "/rule remove <id>\n"
    "/rule clear\n"
    "Events: issue.opened, pr.merged, release, comment, … · "
    "filters: label:bug,urgent · author:x · base:main · "
    "title:text or title:/regex/ · draft:false · milestone:v2 · assignee:y. "
    "See /rules to list them."
)
REPLY_RULE_ADDED: Final = "Rule added for {scope}: {desc}"
REPLY_RULE_REMOVED: Final = "Rule {id} removed from {scope}."
REPLY_RULE_UNKNOWN: Final = "No rule {id} at {scope}. Use /rules to list them."
REPLY_RULES_CLEARED: Final = "Cleared {count} rule(s) for {scope}."
REPLY_RULES_NONE: Final = "No rules set for {scope}. Add one, e.g. /rule add pr.merged"
REPLY_RULES_LIST: Final = "Rules for {scope}:\n{lines}"
REPLY_RULES_FULL: Final = (
    "You already have the maximum of {max} rules at {scope}; remove one with /rule remove <id>."
)
REPLY_LLM_USAGE: Final = (
    "Usage: /llm show · /llm set key:value … · /llm reset\n"
    "Keys: platform, model (default|large), lang (output language), "
    "temp (0..1), max_tokens"
)
REPLY_LLM_OFF_DEPLOY: Final = "LLM analyses aren't enabled on this deployment; ask the operator."
REPLY_LLM_SHOW: Final = "LLM settings for {scope} ({origin}):\n{line}"
REPLY_LLM_SET: Final = "LLM settings updated for {scope}: {line}"
REPLY_LLM_RESET: Final = "LLM settings for {scope} back to deployment defaults."
REPLY_ACTION_USAGE: Final = (
    "Usage:\n"
    "/action add <schedule> <recipe> [key=value …]\n"
    "/action remove <id>\n"
    "/action list\n"
    "Schedules: every:<N>h · daily@HH:MM · weekly@<dow>.HH:MM (UTC). "
    "Recipes: summarize, talking_points, stats, stats_narrative, prompt:<template>. "
    "Params: window=24h|7d · page=<title> · model=default|large · "
    "lang=<code> · temp=<0..1>"
)
REPLY_ACTIONS_OFF_DEPLOY: Final = (
    "Scheduled analyses aren't enabled on this deployment; ask the operator."
)
REPLY_ACTION_ADDED: Final = "Scheduled for {scope}: {desc}"
REPLY_ACTION_REMOVED: Final = "Action {id} removed from {scope}."
REPLY_ACTION_UNKNOWN: Final = "No action {id} at {scope}. Use /action list to see them."
REPLY_ACTIONS_NONE: Final = (
    "No scheduled actions for {scope}. Add one, e.g. /action add daily@06:00 summarize"
)
REPLY_ACTIONS_LIST: Final = "Scheduled actions for {scope}:\n{lines}"
REPLY_ACTIONS_FULL: Final = (
    "You already have the maximum of {max} actions at {scope}; remove one with /action remove <id>."
)
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
SETTINGS_TEMPLATE: Final = (
    "Configuration for {scope}{customized}:\n"
    "- /log publishes to: {log_page}\n"
    "- consent policy: {consent}\n"
    "- GitHub repo: {repo}\n"
    "- repo token stored: {token}\n"
    "- repo notifications: {events}\n"
    "- message capture: {capture}"
)

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
    vault: TokenVault | None
    # The neutral service behind the two shared commands (/setpage, /capture
    # on|off). Its capture_service mirrors this handler's own.
    commands: CommandService
    # Capture-enabled deployments only: the archive behind /capture purge
    # and the ingest service whose policy cache /capture must invalidate.
    archive: MessageArchive | None = None
    capture_service: CaptureService | None = None
    # Analysis-enabled deployments only: the /llm defaults and hard cap,
    # plus the action store and clock behind /action scheduling.
    llm_defaults: LlmSettings | None = None
    llm_max_tokens_ceiling: int = 4096
    actions: ActionStore | None = None
    clock: Clock | None = None

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
        if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repo) or ".." in repo:
            await self._reply(context, chat_id, thread_id, REPLY_SETREPO_USAGE)
            return
        try:
            await self.directory.set_repo(scope, repo)
            if self.vault is not None:
                # A token consented for the previous repo must never be
                # replayed against the new one.
                await self.vault.delete_token(scope)
        except StorageError:
            await self._reply(context, chat_id, thread_id, REPLY_STORAGE_DOWN)
            return
        log_event("profile_update", "ok")
        nonce = self.binding.mint_link(scope)
        link = f"https://t.me/{context.bot.username}?start=cfg_{nonce}"
        await self._reply(
            context,
            chat_id,
            thread_id,
            REPLY_REPO_BOUND.format(repo=repo, link=link, scope=_scope(scope)),
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
        """Discard this group's stored API token."""
        scope = await self._admin_chat(update, context)
        if scope is None:
            return
        chat_id, thread_id = _target(scope)
        if self.vault is None:
            await self._reply(context, chat_id, thread_id, REPLY_SELF_SERVICE_OFF)
            return
        try:
            await self.vault.delete_token(scope)
        except StorageError:
            await self._reply(context, chat_id, thread_id, REPLY_STORAGE_DOWN)
            return
        log_event("token_revoked", "ok")
        await self._reply(context, chat_id, thread_id, REPLY_PAT_REVOKED)

    async def on_events(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Switch this (group, topic)'s rule-driven notifications on or off.

        ``on`` requires a repo bound at this scope and seeds two starter
        rules when none exist, so it works out of the box; the rules
        themselves decide what is delivered.
        """
        scope = await self._admin_chat(update, context)
        if scope is None:
            return
        chat_id, thread_id = _target(scope)
        argument = (context.args or [""])[0].lower()
        if argument not in {"on", "off"}:
            await self._reply(context, chat_id, thread_id, REPLY_EVENTS_USAGE)
            return
        try:
            reply = await self._toggle_events(scope, enabled=argument == "on")
        except StorageError:
            reply = REPLY_STORAGE_DOWN
        await self._reply(context, chat_id, thread_id, reply)

    async def _toggle_events(self, scope: Scope, *, enabled: bool) -> str:
        if not enabled:
            await self.directory.set_events(scope, enabled=False)
            log_event("profile_update", "ok")
            return REPLY_EVENTS_SET.format(state="off")
        own = await self.directory.profile_of(scope)
        if not own.repo:
            # A topic that only inherits the group repo can't get its own
            # notifications — the notifier polls per bound-repo row.
            return REPLY_EVENTS_NEED_REPO
        seed = tuple(parse_rule(spec) for spec in DEFAULT_RULES)
        seeded = await self.directory.enable_events(scope, seed)
        log_event("profile_update", "ok")
        return REPLY_EVENTS_SEEDED if seeded else REPLY_EVENTS_SET.format(state="on")

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
        actions, clock = self.actions, self.clock
        if actions is None or clock is None:
            await self._reply(context, chat_id, thread_id, REPLY_ACTIONS_OFF_DEPLOY)
            return
        args = list(context.args or ())
        sub = args[0].lower() if args else ""
        try:
            if sub == "add" and len(args) > 1:
                reply = await self._action_add(scope, args[1:], actions, clock)
            elif sub == "remove" and len(args) == 2:  # noqa: PLR2004 -- "remove <id>"
                reply = await self._action_remove(scope, args[1], actions)
            elif sub == "list":
                reply = await self._action_list(scope, actions)
            else:
                reply = REPLY_ACTION_USAGE
        except StorageError:
            reply = REPLY_STORAGE_DOWN
        await self._reply(context, chat_id, thread_id, reply)

    async def _action_add(
        self, scope: Scope, tokens: list[str], actions: ActionStore, clock: Clock
    ) -> str:
        try:
            spec = parse_action(" ".join(tokens), now_iso=clock.now().isoformat())
        except ActionParseError as error:
            return str(error)
        current = await actions.get_actions(scope)
        if len(current) >= MAX_ACTIONS:
            return REPLY_ACTIONS_FULL.format(max=MAX_ACTIONS, scope=_scope(scope))
        await actions.set_actions(scope, (*current, spec))
        self.counters.increment("actions_configured")
        log_event("profile_update", "ok")
        return REPLY_ACTION_ADDED.format(scope=_scope(scope), desc=describe_action(spec))

    async def _action_remove(self, scope: Scope, action_id: str, actions: ActionStore) -> str:
        current = await actions.get_actions(scope)
        kept = tuple(spec for spec in current if spec.action_id != action_id)
        if len(kept) == len(current):
            return REPLY_ACTION_UNKNOWN.format(id=action_id, scope=_scope(scope))
        await actions.set_actions(scope, kept)
        log_event("profile_update", "ok")
        return REPLY_ACTION_REMOVED.format(id=action_id, scope=_scope(scope))

    async def _action_list(self, scope: Scope, actions: ActionStore) -> str:
        current = await actions.get_actions(scope)
        if not current:
            return REPLY_ACTIONS_NONE.format(scope=_scope(scope))
        lines = "\n".join(describe_action(spec) for spec in current)
        return REPLY_ACTIONS_LIST.format(scope=_scope(scope), lines=lines)

    async def on_llm(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Show, set, or reset this scope's LLM settings (v3 §2.4)."""
        scope = await self._admin_chat(update, context)
        if scope is None:
            return
        chat_id, thread_id = _target(scope)
        defaults = self.llm_defaults
        if defaults is None:
            await self._reply(context, chat_id, thread_id, REPLY_LLM_OFF_DEPLOY)
            return
        args = list(context.args or ())
        sub = args[0].lower() if args else ""
        try:
            if sub == "show":
                reply = await self._llm_show(scope, defaults)
            elif sub == "set" and len(args) > 1:
                reply = await self._llm_set(scope, args[1:], defaults)
            elif sub == "reset":
                await self.directory.set_llm(scope, None)
                log_event("profile_update", "ok")
                reply = REPLY_LLM_RESET.format(scope=_scope(scope))
            else:
                reply = REPLY_LLM_USAGE
        except StorageError:
            reply = REPLY_STORAGE_DOWN
        await self._reply(context, chat_id, thread_id, reply)

    async def _effective_llm(self, scope: Scope, defaults: LlmSettings) -> tuple[LlmSettings, str]:
        """Resolve topic override → group default → deployment defaults."""
        own = await self.directory.profile_of(scope)
        if own.llm:
            return own.llm, "set for this scope"
        if scope.thread:
            group = await self.directory.profile_of(replace(scope, thread=""))
            if group.llm:
                return group.llm, "inherited from the group"
        return defaults, "deployment defaults"

    async def _llm_show(self, scope: Scope, defaults: LlmSettings) -> str:
        settings, origin = await self._effective_llm(scope, defaults)
        return REPLY_LLM_SHOW.format(
            scope=_scope(scope), origin=origin, line=describe_llm(settings)
        )

    async def _llm_set(self, scope: Scope, tokens: list[str], defaults: LlmSettings) -> str:
        # Partial edits build on what the scope actually runs with — for
        # a topic that inherits, that's the group settings, not the
        # deployment defaults (otherwise `set temp:0.4` would silently
        # reset an inherited model/lang).
        base, _ = await self._effective_llm(scope, defaults)
        try:
            settings = parse_llm_args(" ".join(tokens), base, self.llm_max_tokens_ceiling)
        except LlmParseError as error:
            return str(error)
        await self.directory.set_llm(scope, settings)
        log_event("profile_update", "ok")
        return REPLY_LLM_SET.format(scope=_scope(scope), line=describe_llm(settings))

    async def on_rule(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Add, remove, or clear this (group, topic)'s composable event rules."""
        scope = await self._admin_chat(update, context)
        if scope is None:
            return
        chat_id, thread_id = _target(scope)
        args = list(context.args or ())
        sub = args[0].lower() if args else ""
        try:
            if sub == "add":
                reply = await self._rule_add(scope, args[1:])
            elif sub == "remove" and len(args) == 2:  # noqa: PLR2004 -- "remove <id>"
                reply = await self._rule_remove(scope, args[1])
            elif sub == "clear":
                reply = await self._rule_clear(scope)
            else:
                reply = REPLY_RULE_USAGE
        except StorageError:
            reply = REPLY_STORAGE_DOWN
        await self._reply(context, chat_id, thread_id, reply)

    async def _rule_add(self, scope: Scope, tokens: list[str]) -> str:
        try:
            rule = parse_rule(" ".join(tokens))
        except RuleParseError as error:
            return str(error)
        try:
            await self.directory.add_rule(scope, rule)
        except TooManyRulesError:
            return REPLY_RULES_FULL.format(max=MAX_RULES, scope=_scope(scope))
        log_event("profile_update", "ok")
        return REPLY_RULE_ADDED.format(scope=_scope(scope), desc=describe_rule(rule))

    async def _rule_remove(self, scope: Scope, rule_id: str) -> str:
        if not await self.directory.remove_rule(scope, rule_id):
            return REPLY_RULE_UNKNOWN.format(id=rule_id, scope=_scope(scope))
        log_event("profile_update", "ok")
        return REPLY_RULE_REMOVED.format(id=rule_id, scope=_scope(scope))

    async def _rule_clear(self, scope: Scope) -> str:
        count = await self.directory.clear_rules(scope)
        log_event("profile_update", "ok")
        return REPLY_RULES_CLEARED.format(count=count, scope=_scope(scope))

    async def on_rules(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """List this (group, topic)'s event rules with their ids."""
        scope = await self._admin_chat(update, context)
        if scope is None:
            return
        chat_id, thread_id = _target(scope)
        try:
            rules = await self.directory.list_rules(scope)
        except StorageError:
            text = REPLY_STORAGE_DOWN
        else:
            if rules:
                lines = "\n".join(describe_rule(rule) for rule in rules)
                text = REPLY_RULES_LIST.format(scope=_scope(scope), lines=lines)
            else:
                text = REPLY_RULES_NONE.format(scope=_scope(scope))
        await self._reply(context, chat_id, thread_id, text)

    async def on_settings(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Show this group's effective configuration."""
        scope = await self._admin_chat(update, context)
        if scope is None:
            return
        chat_id, thread_id = _target(scope)
        settings = await self.directory.resolve(scope)
        own = await self.directory.profile_of(scope)
        text = SETTINGS_TEMPLATE.format(
            scope=_scope(scope),
            customized="" if settings.customized else " (all defaults)",
            log_page=self.page_url_for(settings.log_page),
            consent=settings.consent_mode.value,
            repo=settings.repo or "none",
            token="yes" if settings.has_token else "no",
            events=(f"on ({len(own.rules)} rule(s))" if own.events_enabled else "off"),
            capture="on" if own.capture_enabled else "off",
        )
        await self._reply(context, chat_id, thread_id, text)

    async def on_reset(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Forget this group's profile entirely."""
        scope = await self._admin_chat(update, context)
        if scope is None:
            return
        chat_id, thread_id = _target(scope)
        try:
            await self.directory.reset(scope)
        except StorageError:
            await self._reply(context, chat_id, thread_id, REPLY_STORAGE_DOWN)
            return
        log_event("profile_reset", "ok")
        await self._reply(context, chat_id, thread_id, REPLY_RESET.format(scope=_scope(scope)))

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
