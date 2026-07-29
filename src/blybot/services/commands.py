"""The platform-neutral command service (issue #32).

Both the Telegram and Discord adapters implement the same admin commands.
This service holds that business logic once, so each adapter only maps its
native trigger to a neutral call and renders the returned
:class:`~blybot.domain.models.CommandResult`. It now covers ``capture
on|off``, ``setpage``, ``settings``, ``reset``, ``revoke``, ``llm`` (#43),
and the repo-notification surface — ``events on|off``, ``rule
add|remove|clear`` and ``rules`` (#40).

The rule surface is offered in both shapes a platform router can want: a
flat-token :meth:`CommandService.rule` dispatcher for adapters handed raw
argv, and the individual :meth:`CommandService.add_rule` /
:meth:`~CommandService.remove_rule` / :meth:`~CommandService.clear_rules`
methods for adapters with native subcommands. The grammar is shared either
way — no adapter re-implements the dispatch.

The reply wording lives here as module constants: one neutral phrasing per
outcome, consolidating what the two adapters used to word slightly
differently. Nothing platform-specific survives into these strings — no
Telegram "topic"/"group" scope label, no ``/log`` reference, no Discord
"server" — so both adapters can send them verbatim. ``/setpage`` and
``/capture`` are shared command names on both platforms, so naming them is
neutral; the ``/{suffix}`` leaf is filled from the directory's configured
page suffix.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Final

from blybot.domain.models import CommandResult
from blybot.domain.ports import StorageError
from blybot.observability import log_event
from blybot.services.directory import (
    PageNotAllowedError,
    SelfServiceUnavailableError,
    TooManyRulesError,
)
from blybot.services.llmconf import LlmParseError, describe_llm, parse_llm_args
from blybot.services.rules import (
    MAX_RULES,
    RuleParseError,
    default_rules,
    describe_rule,
    parse_rule,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from blybot.domain.models import LlmSettings, Scope
    from blybot.domain.ports import RepoActions, TokenVault
    from blybot.observability import Counters
    from blybot.services.capture import CaptureService
    from blybot.services.directory import ChannelDirectory
    from blybot.services.policy import GroupPolicy

REPLY_NOT_ADMIN: Final = "Only this chat's admins can run that command."
REPLY_NOT_ALLOWED: Final = "I'm not configured to serve this channel."
REPLY_STORAGE_DOWN: Final = "Configuration is temporarily unavailable — please try again later."
REPLY_CAPTURE_OFF_DEPLOY: Final = (
    "Message capture isn't enabled on this deployment; ask the operator."
)
REPLY_CAPTURE_ENABLED: Final = (
    "📢 Message capture is now ON here: messages will be archived for on-wiki summaries "
    "and statistics, with authors recorded only as anonymous labels. An admin can stop "
    "this with /capture off."
)
REPLY_CAPTURE_DISABLED: Final = "Message capture is OFF here. The existing archive is kept."
REPLY_SELF_SERVICE_OFF: Final = (
    "Self-service configuration isn't enabled on this deployment; ask the operator."
)
REPLY_PAGE_REFUSED: Final = (
    "That page path isn't valid — give a plain project or user page, "
    'e.g. /setpage WikiProject Foo (I add the "/{suffix}" leaf myself).'
)
REPLY_SETPAGE_USAGE: Final = (
    "Usage: /setpage <page path> — I publish under <path>/{suffix}, e.g. /setpage WikiProject Foo"
)
REPLY_PAGE_SET: Final = "Done. This scope now publishes to {url}."
REPLY_RESET: Final = "Forgotten. This scope is back on the inherited defaults."
REPLY_REVOKED: Final = "Token discarded. Repo commands are disabled here."
REPLY_LLM_USAGE: Final = (
    "Usage: /llm show · /llm set key:value … · /llm reset\n"
    "Keys: platform, model (default|large), lang (output language), "
    "temp (0..1), max_tokens"
)
REPLY_LLM_OFF_DEPLOY: Final = "LLM analyses aren't enabled on this deployment; ask the operator."
REPLY_LLM_SHOW: Final = "LLM settings ({origin}):\n{line}"
REPLY_LLM_SET: Final = "LLM settings updated: {line}"
REPLY_LLM_RESET: Final = "LLM settings back to deployment defaults."
# Where the effective /llm settings came from — neutral (no "topic"/"group").
_LLM_ORIGIN_OWN: Final = "set for this scope"
_LLM_ORIGIN_INHERITED: Final = "inherited from the parent scope"
_LLM_ORIGIN_DEFAULT: Final = "deployment defaults"
# GitHub owner/name, deliberately strict: no path traversal, no nesting.
_REPO_PATTERN: Final = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+")
REPLY_SETREPO_USAGE: Final = "Usage: /setrepo owner/repository"
REPLY_REPO_BOUND: Final = (
    "Repo bound for this scope: {repo}. Any previously stored token was discarded."
)
REPLY_PAT_OFF_DEPLOY: Final = "Repo tokens aren't enabled on this deployment; ask the operator."
REPLY_PAT_NO_REPO: Final = "No repository is bound at this scope; run /setrepo here first."
REPLY_PAT_MISSING: Final = "No token supplied."
REPLY_PAT_INVALID: Final = (
    "GitHub rejected that token for the bound repository — check the repository "
    "access and the Issues permission, then try again."
)
REPLY_PAT_STORE_FAILED: Final = (
    "Storing the token failed on my side — please try again in a moment."
)
REPLY_PAT_SAVED: Final = (
    "Token validated, encrypted and stored. /issue and /repo are live for this "
    "scope; /revoke discards the token."
)
REPLY_EVENTS_USAGE: Final = "Usage: /events on | off — rules decide what's delivered (see /rules)"
REPLY_EVENTS_SET: Final = "Repo notifications: {state}."
REPLY_EVENTS_NEED_REPO: Final = (
    "Bind a repository at this scope first — /setrepo owner/repo — then /events on."
)
REPLY_EVENTS_SEEDED: Final = (
    "Repo notifications on. Seeded two starter rules — pr.merged and release, "
    "both digest. Use /rules to see them, /rule to add more, /rule clear to start over."
)
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
REPLY_RULE_ADDED: Final = "Rule added for this scope: {desc}"
REPLY_RULE_REMOVED: Final = "Rule {id} removed from this scope."
REPLY_RULE_UNKNOWN: Final = "No rule {id} at this scope. Use /rules to list them."
REPLY_RULES_CLEARED: Final = "Cleared {count} rule(s) for this scope."
REPLY_RULES_NONE: Final = "No rules set for this scope. Add one, e.g. /rule add pr.merged"
REPLY_RULES_LIST: Final = "Rules for this scope:\n{lines}"
REPLY_RULES_FULL: Final = (
    "You already have the maximum of {max} rules at this scope; remove one with /rule remove <id>."
)
REPLY_SETTINGS: Final = (
    "Configuration{customized}:\n"
    "- publishes to: {log_page}\n"
    "- consent policy: {consent}\n"
    "- repo: {repo}\n"
    "- repo token stored: {token}\n"
    "- repo notifications: {events}\n"
    "- message capture: {capture}\n"
    "- digest subscriptions: {subscribable}\n"
    "- LLM settings: {llm}"
)


@dataclass(eq=False)
class CommandService:
    """Neutral business logic behind the shared admin commands.

    Holds only neutral dependencies: the settings :class:`ChannelDirectory`,
    the :class:`GroupPolicy` admission check, the ``page_url_for`` renderer,
    the operator ``counters``, and — on capture-enabled deployments — the
    :class:`CaptureService` whose policy cache a toggle must invalidate.
    ``capture_service`` is ``None`` on a deployment without an archive, in
    which case ``/capture`` fails closed to the off-deployment result.
    """

    directory: ChannelDirectory
    groups: GroupPolicy
    page_url_for: Callable[[str], str]
    counters: Counters
    capture_service: CaptureService | None = None
    # /revoke and the token flow need the vault; None on a deployment
    # without one, in which case both fail closed.
    vault: TokenVault | None = None
    # Only the token flow needs this: it validates a pasted token against
    # the bound repo before storing it. None disables /setrepo's token half.
    repo_actions: RepoActions | None = None
    # Analysis-enabled deployments only: the /llm defaults and hard cap.
    # ``llm_defaults`` is None when LLM analyses are off, in which case
    # /llm fails closed to the off-deployment result. The ceiling default
    # is a placeholder — the composition root always supplies the real one
    # whenever ``llm_defaults`` is set (a bare literal here would also trip
    # the "no hard-coded size" arch guard).
    llm_defaults: LlmSettings | None = None
    llm_max_tokens_ceiling: int = 1024

    async def capture(self, scope: Scope, *, is_admin: bool, enabled: bool) -> CommandResult:
        """Turn this scope's message capture on or off (admins only)."""
        if not is_admin:
            return CommandResult(REPLY_NOT_ADMIN, ok=False)
        service = self.capture_service
        if service is None:
            return CommandResult(REPLY_CAPTURE_OFF_DEPLOY, ok=False)
        if not self.groups.is_allowed(scope):
            return CommandResult(REPLY_NOT_ALLOWED, ok=False)
        try:
            await self.directory.set_capture(scope, enabled=enabled)
            service.forget_scope(scope)
        except StorageError:
            if not enabled:
                # The durable disable never landed: tombstone the scope so
                # the maintenance tick converges the revocation instead of
                # resuming off the stale row. `on` already fails safe.
                service.deny_scope(scope)
            return CommandResult(REPLY_STORAGE_DOWN, ok=False)
        self.counters.increment("profiles_configured")
        log_event("profile_update", "ok")
        # The ON confirmation *is* the permanent in-chat announcement.
        return CommandResult(REPLY_CAPTURE_ENABLED if enabled else REPLY_CAPTURE_DISABLED)

    async def set_page(self, scope: Scope, *, is_admin: bool, page: str) -> CommandResult:
        """Point this scope's analyses at a wiki page (admins only)."""
        if not is_admin:
            return CommandResult(REPLY_NOT_ADMIN, ok=False)
        title = page.strip()
        if not title:
            return CommandResult(
                REPLY_SETPAGE_USAGE.format(suffix=self.directory.page_suffix), ok=False
            )
        try:
            normalized = await self.directory.set_log_page(scope, title)
        except PageNotAllowedError:
            return CommandResult(
                REPLY_PAGE_REFUSED.format(suffix=self.directory.page_suffix), ok=False
            )
        except SelfServiceUnavailableError:
            return CommandResult(REPLY_SELF_SERVICE_OFF, ok=False)
        except StorageError:
            return CommandResult(REPLY_STORAGE_DOWN, ok=False)
        self.counters.increment("profiles_configured")
        log_event("profile_update", "ok")
        return CommandResult(REPLY_PAGE_SET.format(url=self.page_url_for(normalized)))

    async def show_settings(self, scope: Scope, *, is_admin: bool) -> CommandResult:
        """Report this scope's effective configuration (admins only, read-only)."""
        if not is_admin:
            return CommandResult(REPLY_NOT_ADMIN, ok=False)
        try:
            settings = await self.directory.resolve(scope)
            own = await self.directory.profile_of(scope)
        except SelfServiceUnavailableError:
            return CommandResult(REPLY_SELF_SERVICE_OFF, ok=False)
        except StorageError:
            return CommandResult(REPLY_STORAGE_DOWN, ok=False)
        text = REPLY_SETTINGS.format(
            customized="" if settings.customized else " (all defaults)",
            log_page=self.page_url_for(settings.log_page),
            consent=settings.consent_mode.value,
            repo=settings.repo or "none",
            token="yes" if settings.has_token else "no",
            events=(f"on ({len(own.rules)} rule(s))" if own.events_enabled else "off"),
            capture="on" if own.capture_enabled else "off",
            subscribable="on" if own.subscribe_code else "off",
            llm=describe_llm(own.llm) if own.llm else "deployment defaults",
        )
        return CommandResult(text)

    async def reset(self, scope: Scope, *, is_admin: bool) -> CommandResult:
        """Forget this scope's profile, returning it to inherited defaults (admins only)."""
        if not is_admin:
            return CommandResult(REPLY_NOT_ADMIN, ok=False)
        try:
            await self.directory.reset(scope)
        except SelfServiceUnavailableError:
            return CommandResult(REPLY_SELF_SERVICE_OFF, ok=False)
        except StorageError:
            return CommandResult(REPLY_STORAGE_DOWN, ok=False)
        log_event("profile_reset", "ok")
        return CommandResult(REPLY_RESET)

    async def revoke_token(self, scope: Scope, *, is_admin: bool) -> CommandResult:
        """Discard this scope's stored API token (admins only)."""
        if not is_admin:
            return CommandResult(REPLY_NOT_ADMIN, ok=False)
        vault = self.vault
        if vault is None:
            return CommandResult(REPLY_SELF_SERVICE_OFF, ok=False)
        try:
            await vault.delete_token(scope)
        except StorageError:
            return CommandResult(REPLY_STORAGE_DOWN, ok=False)
        log_event("token_revoked", "ok")
        return CommandResult(REPLY_REVOKED)

    async def set_repo(self, scope: Scope, *, is_admin: bool, repo: str) -> CommandResult:
        """Bind this scope to a GitHub repository (admins only).

        Binding always discards any token stored for this scope: a token
        an admin consented to for repository A must never be replayed
        against repository B. Collecting the *new* token is the adapter's
        job — the mechanism (deep link, modal, …) is platform-specific —
        but :meth:`store_token` receives it back here.
        """
        if not is_admin:
            return CommandResult(REPLY_NOT_ADMIN, ok=False)
        candidate = repo.strip()
        if not _REPO_PATTERN.fullmatch(candidate) or ".." in candidate:
            return CommandResult(REPLY_SETREPO_USAGE, ok=False)
        try:
            await self.directory.set_repo(scope, candidate)
            if self.vault is not None:
                await self.vault.delete_token(scope)
        except SelfServiceUnavailableError:
            return CommandResult(REPLY_SELF_SERVICE_OFF, ok=False)
        except StorageError:
            return CommandResult(REPLY_STORAGE_DOWN, ok=False)
        self.counters.increment("profiles_configured")
        log_event("profile_update", "ok")
        return CommandResult(REPLY_REPO_BOUND.format(repo=candidate))

    async def store_token(self, scope: Scope, *, is_admin: bool, token: str) -> CommandResult:
        """Validate a repo token against this scope's bound repo and store it.

        The secret-handling half of the ``/setrepo`` flow, shared by every
        platform: how the token was *collected* differs (Telegram's DM
        paste, Discord's modal), but validating it against the bound repo
        and encrypting it into the vault is identical everywhere — so no
        adapter reimplements it.

        ``ok`` is ``False`` for every failure; an adapter running a
        multi-step entry flow may leave its prompt armed for a retry.
        """
        if not is_admin:
            return CommandResult(REPLY_NOT_ADMIN, ok=False)
        actions, vault = self.repo_actions, self.vault
        if actions is None or vault is None:
            return CommandResult(REPLY_PAT_OFF_DEPLOY, ok=False)
        secret = token.strip()
        if not secret:
            return CommandResult(REPLY_PAT_MISSING, ok=False)
        try:
            return await self._validate_and_store(scope, secret, actions, vault)
        except StorageError:
            return CommandResult(REPLY_PAT_STORE_FAILED, ok=False)

    async def _validate_and_store(
        self, scope: Scope, secret: str, actions: RepoActions, vault: TokenVault
    ) -> CommandResult:
        """Check the secret against the bound repo, then encrypt it into the vault."""
        settings = await self.directory.resolve(scope)
        if not settings.repo:
            return CommandResult(REPLY_PAT_NO_REPO, ok=False)
        if not await actions.validate_token(settings.repo, secret):
            return CommandResult(REPLY_PAT_INVALID, ok=False)
        await vault.store_token(scope, secret)
        self.counters.increment("tokens_bound")
        log_event("token_bound", "ok")
        return CommandResult(REPLY_PAT_SAVED)

    async def events(self, scope: Scope, *, is_admin: bool, tokens: list[str]) -> CommandResult:
        """Toggle notifications from a flat ``/events`` token list (admins only).

        The argv-shaped surface, mirroring :meth:`rule`: an adapter handed
        raw arguments delegates the ``on``/``off`` grammar (and the usage
        reply for anything else) instead of re-checking it itself.
        """
        if not is_admin:
            return CommandResult(REPLY_NOT_ADMIN, ok=False)
        argument = tokens[0].lower() if tokens else ""
        if argument not in {"on", "off"}:
            return CommandResult(REPLY_EVENTS_USAGE, ok=False)
        return await self.set_events(scope, is_admin=True, enabled=argument == "on")

    async def set_events(self, scope: Scope, *, is_admin: bool, enabled: bool) -> CommandResult:
        """Switch this scope's rule-driven repo notifications on or off (admins only).

        ``on`` requires a repo bound at this very scope and seeds the
        starter ruleset when none exists, so it works out of the box; the
        rules themselves decide what is delivered.
        """
        if not is_admin:
            return CommandResult(REPLY_NOT_ADMIN, ok=False)
        try:
            return await self._toggle_events(scope, enabled=enabled)
        except SelfServiceUnavailableError:
            return CommandResult(REPLY_SELF_SERVICE_OFF, ok=False)
        except StorageError:
            return CommandResult(REPLY_STORAGE_DOWN, ok=False)

    async def _toggle_events(self, scope: Scope, *, enabled: bool) -> CommandResult:
        if not enabled:
            await self.directory.set_events(scope, enabled=False)
            log_event("profile_update", "ok")
            return CommandResult(REPLY_EVENTS_SET.format(state="off"))
        own = await self.directory.profile_of(scope)
        if not own.repo:
            # A scope that merely inherits its parent's repo can't get its
            # own notifications — the notifier polls per bound-repo row.
            return CommandResult(REPLY_EVENTS_NEED_REPO, ok=False)
        seeded = await self.directory.enable_events(scope, default_rules())
        log_event("profile_update", "ok")
        state = REPLY_EVENTS_SEEDED if seeded else REPLY_EVENTS_SET.format(state="on")
        return CommandResult(state)

    async def rule(self, scope: Scope, *, is_admin: bool, tokens: list[str]) -> CommandResult:
        """Add, remove, or clear rules from a flat ``/rule`` token list (admins only).

        The argv-shaped surface, for platforms whose command router hands
        over the raw arguments. Platforms with native subcommands call
        :meth:`add_rule`, :meth:`remove_rule`, or :meth:`clear_rules`
        directly — the dispatch grammar lives here so neither adapter owns it.
        """
        if not is_admin:
            return CommandResult(REPLY_NOT_ADMIN, ok=False)
        sub = tokens[0].lower() if tokens else ""
        if sub == "add":
            return await self.add_rule(scope, is_admin=True, spec=" ".join(tokens[1:]))
        if sub == "remove" and len(tokens) == 2:  # noqa: PLR2004 -- "remove <id>"
            return await self.remove_rule(scope, is_admin=True, rule_id=tokens[1])
        if sub == "clear":
            return await self.clear_rules(scope, is_admin=True)
        return CommandResult(REPLY_RULE_USAGE, ok=False)

    async def add_rule(self, scope: Scope, *, is_admin: bool, spec: str) -> CommandResult:
        """Append one composable event rule to this scope (admins only)."""
        if not is_admin:
            return CommandResult(REPLY_NOT_ADMIN, ok=False)
        try:
            parsed = parse_rule(spec)
        except RuleParseError as error:
            return CommandResult(str(error), ok=False)
        try:
            await self.directory.add_rule(scope, parsed)
        except TooManyRulesError:
            return CommandResult(REPLY_RULES_FULL.format(max=MAX_RULES), ok=False)
        except SelfServiceUnavailableError:
            return CommandResult(REPLY_SELF_SERVICE_OFF, ok=False)
        except StorageError:
            return CommandResult(REPLY_STORAGE_DOWN, ok=False)
        log_event("profile_update", "ok")
        return CommandResult(REPLY_RULE_ADDED.format(desc=describe_rule(parsed)))

    async def remove_rule(self, scope: Scope, *, is_admin: bool, rule_id: str) -> CommandResult:
        """Drop one rule from this scope by id (admins only)."""
        if not is_admin:
            return CommandResult(REPLY_NOT_ADMIN, ok=False)
        try:
            removed = await self.directory.remove_rule(scope, rule_id)
        except SelfServiceUnavailableError:
            return CommandResult(REPLY_SELF_SERVICE_OFF, ok=False)
        except StorageError:
            return CommandResult(REPLY_STORAGE_DOWN, ok=False)
        if not removed:
            return CommandResult(REPLY_RULE_UNKNOWN.format(id=rule_id), ok=False)
        log_event("profile_update", "ok")
        return CommandResult(REPLY_RULE_REMOVED.format(id=rule_id))

    async def clear_rules(self, scope: Scope, *, is_admin: bool) -> CommandResult:
        """Remove every rule at this scope (admins only)."""
        if not is_admin:
            return CommandResult(REPLY_NOT_ADMIN, ok=False)
        try:
            count = await self.directory.clear_rules(scope)
        except SelfServiceUnavailableError:
            return CommandResult(REPLY_SELF_SERVICE_OFF, ok=False)
        except StorageError:
            return CommandResult(REPLY_STORAGE_DOWN, ok=False)
        log_event("profile_update", "ok")
        return CommandResult(REPLY_RULES_CLEARED.format(count=count))

    async def list_rules(self, scope: Scope, *, is_admin: bool) -> CommandResult:
        """List this scope's event rules with their ids (admins only, read-only)."""
        if not is_admin:
            return CommandResult(REPLY_NOT_ADMIN, ok=False)
        try:
            rules = await self.directory.list_rules(scope)
        except SelfServiceUnavailableError:
            return CommandResult(REPLY_SELF_SERVICE_OFF, ok=False)
        except StorageError:
            return CommandResult(REPLY_STORAGE_DOWN, ok=False)
        if not rules:
            return CommandResult(REPLY_RULES_NONE, ok=False)
        lines = "\n".join(describe_rule(item) for item in rules)
        return CommandResult(REPLY_RULES_LIST.format(lines=lines))

    async def set_llm(self, scope: Scope, *, is_admin: bool, tokens: list[str]) -> CommandResult:
        """Show, set, or reset this scope's LLM settings (admins only).

        ``tokens`` is the raw ``/llm`` argument list: ``show``, ``set
        key:value …``, or ``reset``. The grammar and per-key validation are
        the shared :mod:`blybot.services.llmconf` module's, unchanged.
        """
        if not is_admin:
            return CommandResult(REPLY_NOT_ADMIN, ok=False)
        defaults = self.llm_defaults
        if defaults is None:
            return CommandResult(REPLY_LLM_OFF_DEPLOY, ok=False)
        try:
            return await self._dispatch_llm(scope, tokens, defaults)
        except StorageError:
            return CommandResult(REPLY_STORAGE_DOWN, ok=False)

    async def _dispatch_llm(
        self, scope: Scope, tokens: list[str], defaults: LlmSettings
    ) -> CommandResult:
        """Route a validated (admin, on-deployment) ``/llm`` call by subcommand."""
        sub = tokens[0].lower() if tokens else ""
        if sub == "show":
            return CommandResult(await self._llm_show(scope, defaults))
        if sub == "set" and len(tokens) > 1:
            return await self._llm_set(scope, tokens[1:], defaults)
        if sub == "reset":
            await self.directory.set_llm(scope, None)
            log_event("profile_update", "ok")
            return CommandResult(REPLY_LLM_RESET)
        return CommandResult(REPLY_LLM_USAGE, ok=False)

    async def _effective_llm(self, scope: Scope, defaults: LlmSettings) -> tuple[LlmSettings, str]:
        """Resolve own override → parent-scope default → deployment defaults."""
        own = await self.directory.profile_of(scope)
        if own.llm:
            return own.llm, _LLM_ORIGIN_OWN
        if scope.thread:
            parent = await self.directory.profile_of(replace(scope, thread=""))
            if parent.llm:
                return parent.llm, _LLM_ORIGIN_INHERITED
        return defaults, _LLM_ORIGIN_DEFAULT

    async def _llm_show(self, scope: Scope, defaults: LlmSettings) -> str:
        settings, origin = await self._effective_llm(scope, defaults)
        return REPLY_LLM_SHOW.format(origin=origin, line=describe_llm(settings))

    async def _llm_set(
        self, scope: Scope, tokens: list[str], defaults: LlmSettings
    ) -> CommandResult:
        # Partial edits build on what the scope actually runs with — for a
        # thread that inherits, that's the parent settings, not the
        # deployment defaults (otherwise `set temp:0.4` would silently reset
        # an inherited model/lang).
        base, _origin = await self._effective_llm(scope, defaults)
        try:
            settings = parse_llm_args(" ".join(tokens), base, self.llm_max_tokens_ceiling)
        except LlmParseError as error:
            return CommandResult(str(error), ok=False)
        await self.directory.set_llm(scope, settings)
        log_event("profile_update", "ok")
        return CommandResult(REPLY_LLM_SET.format(line=describe_llm(settings)))
