"""The platform-neutral command service (issue #32).

Both the Telegram and Discord adapters implement the same admin commands.
This service holds that business logic once, so each adapter only maps its
native trigger to a neutral call and renders the returned
:class:`~blybot.domain.models.CommandResult`. It now covers ``capture
on|off``, ``setpage``, ``settings``, ``reset``, ``revoke``, ``llm`` (#43),
the repo-notification surface — ``events on|off``, ``rule
add|remove|clear`` and ``rules`` (#40) — the ``setrepo``/token flow (#43),
the bound-repo commands ``issue`` and ``repo`` (#42), the anonymous
``log`` publish path (#44), and the DM digest surface — ``subscribe``,
``mysubs`` and ``unsubscribe`` (#32).

Every inbound command an adapter serves now resolves here. What stays in an
adapter is the trigger mapping and whatever its platform can do that no
other can: Telegram's per-*user* rate cap (it keys on a raw user id, which
must not cross inward), its deep-link and pending-message handshake, its
``/logmedia`` media fetch and review DM; Discord's modal, context menu, and
subscribable-code mint.

Most commands are admin-gated; ``issue`` and ``repo`` deliberately are not,
since filing anonymously is the one repo action any member may take.

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

from blybot.domain.models import CommandResult, ConsentMode
from blybot.domain.ports import IssueTrackerError, StorageError, WikiWriteError
from blybot.domain.subscriptions import Subscription
from blybot.observability import log_event
from blybot.services.actions import (
    MAX_ACTIONS,
    ActionParseError,
    describe_action,
    parse_action,
)
from blybot.services.directory import (
    PageNotAllowedError,
    SelfServiceUnavailableError,
    TooManyRulesError,
)
from blybot.services.feedback import CONFIRMATION_TEMPLATE
from blybot.services.llmconf import LlmParseError, describe_llm, parse_llm_args
from blybot.services.publish import NothingToPublishError, log_action
from blybot.services.repo import NoRepoBoundError, NoTokenError
from blybot.services.rules import (
    MAX_RULES,
    RuleParseError,
    default_rules,
    describe_rule,
    parse_rule,
)
from blybot.services.subscriptions import (
    MAX_SUBS_PER_USER,
    SubscriptionCapReachedError,
    SubscriptionParseError,
    admit_subscription,
    mint_sub_id,
    parse_subscription,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from blybot.domain.models import LlmSettings, LogContent, PlatformCapabilities, Scope
    from blybot.domain.ports import (
        ActionStore,
        Clock,
        RepoActions,
        SubscriptionStore,
        TokenVault,
    )
    from blybot.observability import Counters
    from blybot.services.capture import CaptureService
    from blybot.services.directory import ChannelDirectory, ChannelSettings
    from blybot.services.engine import ActionEngine
    from blybot.services.policy import GroupPolicy, SlidingWindowLimiter
    from blybot.services.repo import GroupRepoService

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
# Bound-repo service failures that map to a user-facing reply.
_REPO_ERRORS: Final = (NoRepoBoundError, NoTokenError, StorageError, IssueTrackerError)
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
REPLY_LOG_CONFIG_UNAVAILABLE: Final = (
    "I can't confirm this chat's settings right now, so I won't publish. Try again shortly."
)
REPLY_LOG_NO_PAGE: Final = (
    "No log page is set for this scope. An admin needs to run /setpage <page path> here first. "
    "Nothing was published."
)
REPLY_LOG_AUTHOR_ONLY: Final = (
    "This chat's consent policy only lets authors publish their own messages."
)
REPLY_LOG_NOTHING: Final = "There's nothing publishable in that message."
REPLY_LOG_OFF_DEPLOY: Final = "Publishing isn't enabled on this deployment; ask the operator."
REPLY_LOG_WIKI_ERROR: Final = "Sorry, publishing failed. The operator can see details in the logs."
REPLY_SUBS_UNAVAILABLE: Final = "Digest subscriptions aren't available on this deployment."
REPLY_SUBS_NO_DURABLE_DM: Final = "Digest subscriptions aren't available on this platform."
REPLY_SUBSCRIBED: Final = (
    "Subscribed [{sub_id}]: {schedule} {recipe} ({lang}) — digests arrive in your DMs. "
    "/mysubs to review, /unsubscribe {sub_id} to stop."
)
REPLY_SUBS_HEADER: Final = "Your digest subscriptions:"
REPLY_NO_SUBS: Final = "You have no digest subscriptions."
REPLY_UNSUB_USAGE: Final = "Usage: /unsubscribe <id> — see your ids with /mysubs"
REPLY_UNSUBSCRIBED: Final = "Unsubscribed."
REPLY_NO_SUCH_SUB: Final = "No subscription with that id is yours."
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
REPLY_ACTION_ADDED: Final = "Scheduled for this scope: {desc}"
REPLY_ACTION_REMOVED: Final = "Action {id} removed from this scope."
REPLY_ACTION_UNKNOWN: Final = "No action {id} at this scope. Use /action list to see them."
REPLY_ACTIONS_NONE: Final = (
    "No scheduled actions for this scope. Add one, e.g. /action add daily@06:00 summarize"
)
REPLY_ACTIONS_LIST: Final = "Scheduled actions for this scope:\n{lines}"
REPLY_ACTIONS_FULL: Final = (
    "You already have the maximum of {max} actions at this scope; "
    "remove one with /action remove <id>."
)
REPLY_THROTTLED: Final = "Rate limit reached — please try again in a minute."
REPLY_ISSUE_USAGE: Final = "Describe the issue after the command: /issue something is broken"
REPLY_ISSUE_DISABLED: Final = "Repository features aren't enabled on this deployment."
REPLY_ISSUE_UNBOUND: Final = (
    "No repository is bound at this scope — an admin can bind one with /setrepo."
)
REPLY_ISSUE_NO_PAT: Final = (
    "A repository is bound but its token step was never completed — an admin "
    "should run /setrepo here and supply the token."
)
REPLY_ISSUE_FAILED: Final = "Sorry, GitHub refused that — the token may have expired (/setrepo)."
REPLY_REPO_SUMMARY: Final = (
    "{repo}: {count} open items. Recent: {titles}\nhttps://github.com/{repo}/issues"
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


def _repo_error_reply(error: Exception) -> str:
    """One place mapping bound-repo failures to user-facing replies."""
    if isinstance(error, NoRepoBoundError):
        return REPLY_ISSUE_UNBOUND
    if isinstance(error, NoTokenError):
        return REPLY_ISSUE_NO_PAT
    if isinstance(error, IssueTrackerError):
        log_event("group_issue", "error")
    return REPLY_ISSUE_FAILED


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
    # /issue and /repo: the bound-repo service and the per-scope rate cap.
    # None disables those commands / their throttling respectively.
    repo_service: GroupRepoService | None = None
    repo_limiter: SlidingWindowLimiter | None = None
    # /action: the schedule store and the clock that stamps a new action's
    # first due time. Both None on a deployment without scheduled analyses,
    # in which case /action fails closed to the off-deployment result.
    actions: ActionStore | None = None
    clock: Clock | None = None
    # The DM digest surface (#32): the store, the platform's durable-DM
    # capability, and the per-subscriber cap (#23).
    subscriptions: SubscriptionStore | None = None
    capabilities: PlatformCapabilities | None = None
    default_lang: str = "en"
    max_subs_per_user: int = MAX_SUBS_PER_USER
    # /log publishes through the action engine; None on a deployment that
    # never publishes (the engine is always built, so this is for tests).
    engine: ActionEngine | None = None
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

    async def file_issue(self, scope: Scope, *, description: str) -> CommandResult:
        """File an anonymous issue in this scope's bound repo (any member).

        Deliberately **not** admin-gated: filing is the one repo action any
        member may take, and no reporter identity is recorded anywhere (R6).
        Adapters are responsible for not leaking the caller when they render
        the reply — on Discord that means an ephemeral response, since a
        public one would attribute the command in the channel.
        """
        text = description.strip()
        if not text:
            return CommandResult(REPLY_ISSUE_USAGE, ok=False)
        service = self.repo_service
        if service is None:
            return CommandResult(REPLY_ISSUE_DISABLED, ok=False)
        if not self.groups.is_allowed(scope):
            return CommandResult(REPLY_NOT_ALLOWED, ok=False)
        if not self._repo_allowed("issue", scope):
            return CommandResult(REPLY_THROTTLED, ok=False)
        try:
            url = await service.file_issue(scope, text)
        except _REPO_ERRORS as error:
            return CommandResult(_repo_error_reply(error), ok=False)
        self.counters.increment("group_issues_filed")
        log_event("group_issue", "ok")
        return CommandResult(CONFIRMATION_TEMPLATE.format(url=url))

    async def repo_summary(self, scope: Scope) -> CommandResult:
        """Report the bound repository's open-items summary (any member)."""
        service = self.repo_service
        if service is None:
            return CommandResult(REPLY_ISSUE_DISABLED, ok=False)
        if not self.groups.is_allowed(scope):
            return CommandResult(REPLY_NOT_ALLOWED, ok=False)
        if not self._repo_allowed("repo", scope):
            return CommandResult(REPLY_THROTTLED, ok=False)
        try:
            summary = await service.summary(scope)
        except _REPO_ERRORS as error:
            return CommandResult(_repo_error_reply(error), ok=False)
        return CommandResult(
            REPLY_REPO_SUMMARY.format(
                repo=summary.repo,
                count=summary.open_count,
                titles="; ".join(summary.recent_titles) or "none",
            )
        )

    def _repo_allowed(self, kind: str, scope: Scope) -> bool:
        """Whether this scope is under its per-command repo rate cap."""
        return self.repo_limiter is None or self.repo_limiter.allow(kind, int(scope.channel))

    async def log_message(
        self, scope: Scope, *, is_author: bool, content: LogContent
    ) -> CommandResult:
        """Publish one message's content to this scope's wiki page, unattributed.

        The shared core of ``/log``: the fail-closed guards, the consent
        policy, the rate cap, and the publish pipeline. Deliberately **not**
        admin-gated — any member may publish, which is why the guards matter.

        ``is_author`` is resolved by the adapter (the requester authored the
        target), never here: comparing raw platform identities is exactly the
        work that must stay at the boundary. Whether the *requester* is
        hidden afterwards is also the adapter's job, and platform-shaped —
        Telegram deletes the command message, Discord never creates one.
        """
        if not self.groups.is_allowed(scope):
            log_event("log_command", "ignored")
            return CommandResult(REPLY_NOT_ALLOWED, ok=False)
        engine = self.engine
        if engine is None:
            return CommandResult(REPLY_LOG_OFF_DEPLOY, ok=False)
        # resolve() never raises: a storage outage comes back as degraded=True,
        # which _log_decline fails closed on.
        settings = await self.directory.resolve(scope)
        if (decline := self._log_decline(scope, settings, is_author=is_author)) is not None:
            return CommandResult(decline, ok=False)
        try:
            outcome = await engine.run(scope, log_action(settings.log_page), payload=content)
        except NothingToPublishError:
            self.counters.increment("log_declined_media")
            return CommandResult(REPLY_LOG_NOTHING, ok=False)
        except WikiWriteError:
            return CommandResult(REPLY_LOG_WIKI_ERROR, ok=False)
        log_event("log_command", "ok")
        text = "\n".join(message.text for message in outcome.messages)
        # The pipeline's payload rides along: Telegram's /logmedia review DM
        # needs the PublishedLog to know which files were uploaded, and
        # rebuilding that in the adapter would mean re-running the publish.
        return CommandResult(
            text or REPLY_LOG_NOTHING, ok=bool(outcome.messages), payload=outcome.payload
        )

    def _log_decline(
        self, scope: Scope, settings: ChannelSettings, *, is_author: bool
    ) -> str | None:
        """The refusal for this /log, or None to publish. Order is significant.

        The rate check consumes a limiter token, so it runs last — only once
        the free guards have passed.
        """
        if settings.degraded:
            # Fail closed: the consent policy and target page are unknown
            # right now, and publishing on defaults could violate both.
            return REPLY_LOG_CONFIG_UNAVAILABLE
        if self.directory.self_service_enabled and not settings.page_explicit:
            # A self-service scope must choose its own page — never leak its
            # logs onto the shared operator default.
            return REPLY_LOG_NO_PAGE
        if settings.consent_mode is ConsentMode.AUTHOR_ONLY and not is_author:
            self.counters.increment("log_declined_consent")
            return REPLY_LOG_AUTHOR_ONLY
        if not self._repo_allowed("log", scope):
            self.counters.increment("log_throttled")
            return REPLY_THROTTLED
        return None

    async def subscribe(self, source: Scope, dm: Scope, *, options: str) -> CommandResult:
        """Create a durable DM digest subscription to ``source`` (any member).

        How an adapter learns ``source`` differs — Telegram redeems a shared
        link into a pending target, Discord takes the channel the command ran
        in — but parsing, the per-user cap, and the confirmation are the same
        everywhere. Gated on ``durable_dm``: a platform without durable direct
        messages has nowhere to deliver.
        """
        store = self.subscriptions
        if store is None:
            return CommandResult(REPLY_SUBS_UNAVAILABLE, ok=False)
        if self.capabilities is not None and not self.capabilities.durable_dm:
            return CommandResult(REPLY_SUBS_NO_DURABLE_DM, ok=False)
        try:
            schedule, recipe, lang = parse_subscription(options, self.default_lang)
        except SubscriptionParseError as error:
            return CommandResult(str(error), ok=False)
        subscription = Subscription(
            sub_id=mint_sub_id(),
            dm=dm,
            scope=source,
            schedule=schedule,
            recipe=recipe,
            lang=lang,
        )
        try:
            await admit_subscription(store, subscription, self.max_subs_per_user)
        except SubscriptionCapReachedError as error:
            return CommandResult(str(error), ok=False)
        except StorageError:
            return CommandResult(REPLY_STORAGE_DOWN, ok=False)
        log_event("subscription_add", "ok")
        return CommandResult(
            REPLY_SUBSCRIBED.format(
                sub_id=subscription.sub_id, schedule=schedule.token, recipe=recipe, lang=lang
            )
        )

    async def list_subscriptions(self, dm: Scope) -> CommandResult:
        """List the caller's DM digest subscriptions."""
        store = self.subscriptions
        if store is None:
            return CommandResult(REPLY_SUBS_UNAVAILABLE, ok=False)
        try:
            subs = await store.list_for_user(dm)
        except StorageError:
            return CommandResult(REPLY_STORAGE_DOWN, ok=False)
        if not subs:
            return CommandResult(REPLY_NO_SUBS, ok=False)
        lines = [REPLY_SUBS_HEADER]
        lines += [f"[{s.sub_id}] {s.schedule.token} {s.recipe} ({s.lang})" for s in subs]
        return CommandResult("\n".join(lines))

    async def unsubscribe(self, dm: Scope, *, subscription_id: str) -> CommandResult:
        """Remove one of the caller's subscriptions by id."""
        store = self.subscriptions
        if store is None:
            return CommandResult(REPLY_SUBS_UNAVAILABLE, ok=False)
        sub_id = subscription_id.strip()
        if not sub_id:
            return CommandResult(REPLY_UNSUB_USAGE, ok=False)
        try:
            removed = await store.remove(dm, sub_id)
        except StorageError:
            return CommandResult(REPLY_STORAGE_DOWN, ok=False)
        return (
            CommandResult(REPLY_UNSUBSCRIBED)
            if removed
            else CommandResult(REPLY_NO_SUCH_SUB, ok=False)
        )

    async def action(self, scope: Scope, *, is_admin: bool, tokens: list[str]) -> CommandResult:
        """Add, remove, or list this scope's scheduled analyses (admins only).

        The argv-shaped surface, mirroring :meth:`rule`. Platforms with
        native subcommands call :meth:`add_action`, :meth:`remove_action`
        or :meth:`list_actions` directly.
        """
        if not is_admin:
            return CommandResult(REPLY_NOT_ADMIN, ok=False)
        sub = tokens[0].lower() if tokens else ""
        if sub == "add" and len(tokens) > 1:
            return await self.add_action(scope, is_admin=True, spec=" ".join(tokens[1:]))
        if sub == "remove" and len(tokens) == 2:  # noqa: PLR2004 -- "remove <id>"
            return await self.remove_action(scope, is_admin=True, action_id=tokens[1])
        if sub == "list":
            return await self.list_actions(scope, is_admin=True)
        return CommandResult(REPLY_ACTION_USAGE, ok=False)

    async def add_action(self, scope: Scope, *, is_admin: bool, spec: str) -> CommandResult:
        """Schedule one recurring analysis for this scope (admins only)."""
        if not is_admin:
            return CommandResult(REPLY_NOT_ADMIN, ok=False)
        store, clock = self.actions, self.clock
        if store is None or clock is None:
            return CommandResult(REPLY_ACTIONS_OFF_DEPLOY, ok=False)
        try:
            parsed = parse_action(spec, now_iso=clock.now().isoformat())
        except ActionParseError as error:
            return CommandResult(str(error), ok=False)
        try:
            current = await store.get_actions(scope)
            if len(current) >= MAX_ACTIONS:
                return CommandResult(REPLY_ACTIONS_FULL.format(max=MAX_ACTIONS), ok=False)
            await store.set_actions(scope, (*current, parsed))
        except StorageError:
            return CommandResult(REPLY_STORAGE_DOWN, ok=False)
        self.counters.increment("actions_configured")
        log_event("profile_update", "ok")
        return CommandResult(REPLY_ACTION_ADDED.format(desc=describe_action(parsed)))

    async def remove_action(self, scope: Scope, *, is_admin: bool, action_id: str) -> CommandResult:
        """Drop one scheduled analysis by id (admins only)."""
        if not is_admin:
            return CommandResult(REPLY_NOT_ADMIN, ok=False)
        store = self.actions
        if store is None:
            return CommandResult(REPLY_ACTIONS_OFF_DEPLOY, ok=False)
        try:
            current = await store.get_actions(scope)
            kept = tuple(spec for spec in current if spec.action_id != action_id)
            if len(kept) == len(current):
                return CommandResult(REPLY_ACTION_UNKNOWN.format(id=action_id), ok=False)
            await store.set_actions(scope, kept)
        except StorageError:
            return CommandResult(REPLY_STORAGE_DOWN, ok=False)
        log_event("profile_update", "ok")
        return CommandResult(REPLY_ACTION_REMOVED.format(id=action_id))

    async def list_actions(self, scope: Scope, *, is_admin: bool) -> CommandResult:
        """List this scope's scheduled analyses (admins only, read-only)."""
        if not is_admin:
            return CommandResult(REPLY_NOT_ADMIN, ok=False)
        store = self.actions
        if store is None:
            return CommandResult(REPLY_ACTIONS_OFF_DEPLOY, ok=False)
        try:
            current = await store.get_actions(scope)
        except StorageError:
            return CommandResult(REPLY_STORAGE_DOWN, ok=False)
        if not current:
            return CommandResult(REPLY_ACTIONS_NONE, ok=False)
        lines = "\n".join(describe_action(spec) for spec in current)
        return CommandResult(REPLY_ACTIONS_LIST.format(lines=lines))

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
