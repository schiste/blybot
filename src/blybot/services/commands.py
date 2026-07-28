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

from dataclasses import dataclass
from typing import TYPE_CHECKING, Final

from blybot.domain.models import CommandResult
from blybot.domain.ports import StorageError
from blybot.observability import log_event
from blybot.services.directory import PageNotAllowedError, SelfServiceUnavailableError

if TYPE_CHECKING:
    from collections.abc import Callable

    from blybot.domain.models import Scope
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
