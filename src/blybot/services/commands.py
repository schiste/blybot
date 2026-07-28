"""The platform-neutral command service (issue #32, increment 1).

Both the Telegram and Discord adapters already implement a handful of the
same admin commands. This service holds that business logic once, so each
adapter only maps its native trigger to a neutral call and renders the
returned :class:`~blybot.domain.models.CommandResult`. This increment
covers the two purely-delegating shared commands — ``capture on|off`` and
``setpage`` — and establishes the contract the feature issues (#40-44)
extend.

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

from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Final

from blybot.domain.models import CommandResult
from blybot.domain.ports import StorageError
from blybot.observability import log_event
from blybot.services.directory import PageNotAllowedError, SelfServiceUnavailableError
from blybot.services.llmconf import LlmParseError, describe_llm, parse_llm_args

if TYPE_CHECKING:
    from collections.abc import Callable

    from blybot.domain.models import LlmSettings, Scope
    from blybot.domain.ports import TokenVault
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
    # Only /revoke needs the vault; None on a deployment without one, in
    # which case /revoke fails closed to the self-service-off result.
    vault: TokenVault | None = None
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
