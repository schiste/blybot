"""Resolves each group's effective settings (self-service, spec v2).

Two-tier resolution: a group's stored :class:`GroupProfile` overrides
the operator's environment defaults, field by field. No profile — or no
profile *store*, the pure v1 deployment — means every group simply gets
the defaults, so single-tenant behavior is the degenerate case, not a
special one.

Reads are failure-tolerant by design: if ToolsDB is down, ``resolve``
returns defaults (``/log`` keeps working); only configuration *writes*
surface :class:`~blybot.domain.ports.StorageError` to the admin.

Wiki safety: ``/setpage`` takes any base page path and always writes to
its ``/{page_suffix}`` subpage (default ``Telegram logs``). A group
picks *where* its log lives, but the leaf is always a clearly-named
logs subpage — the shared wiki account can never be pointed at a bare
content page.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any, Final

from blybot.domain.models import ConsentMode, EventKind, GroupProfile
from blybot.domain.ports import StorageError

if TYPE_CHECKING:
    from blybot.domain.ports import ProfileStore

# Characters MediaWiki forbids in page titles (underscores are handled
# by normalization: they are equivalent to spaces).
_FORBIDDEN_TITLE_CHARS: Final = frozenset('#<>[]|{}"')


class SelfServiceUnavailableError(Exception):
    """Self-service is not enabled on this deployment (no store / no prefix)."""


class PageNotAllowedError(Exception):
    """The requested page is outside the operator's allowed prefix or invalid."""


@dataclass(frozen=True, slots=True)
class ChannelSettings:
    """The effective, fully-resolved settings for one chat."""

    log_page: str
    consent_mode: ConsentMode
    repo: str
    has_token: bool
    events_enabled: bool
    event_kinds: frozenset[EventKind]
    customized: bool  # whether a stored profile contributed anything
    degraded: bool = False  # storage was unreachable: these are fallbacks


@dataclass(eq=False)
class ChannelDirectory:
    """Per-chat settings: stored profile over environment defaults."""

    store: ProfileStore | None
    default_log_page: str
    default_consent: ConsentMode
    default_repo: str
    page_suffix: str  # leaf appended to every /setpage base; "" disables it

    @property
    def self_service_enabled(self) -> bool:
        """Whether groups can configure themselves at all."""
        return self.store is not None

    async def resolve(self, chat_id: int) -> ChannelSettings:
        """Return the chat's effective settings; never raises.

        A storage outage degrades to the environment defaults — the
        store already logged the failure, and publishing must not stop.
        """
        profile: GroupProfile | None = None
        degraded = False
        if self.store is not None:
            try:
                profile = await self.store.get(chat_id)
            except StorageError:
                degraded = True
        if profile is None:
            return ChannelSettings(
                log_page=self.default_log_page,
                consent_mode=self.default_consent,
                repo=self.default_repo,
                has_token=False,
                events_enabled=False,
                event_kinds=frozenset(),
                customized=False,
                degraded=degraded,
            )
        return ChannelSettings(
            log_page=profile.log_page or self.default_log_page,
            consent_mode=profile.consent_mode or self.default_consent,
            repo=profile.repo or self.default_repo,
            has_token=profile.has_token,
            events_enabled=profile.events_enabled,
            event_kinds=profile.event_kinds,
            customized=True,
        )

    async def set_log_page(self, chat_id: int, title: str) -> str:
        """Point the chat's ``/log`` at a page under the allowed prefix.

        ``title`` is a base page path; the log lands on its
        ``/{page_suffix}`` subpage. Returns the full page title. Raises
        :class:`SelfServiceUnavailableError` when page targeting is off,
        :class:`PageNotAllowedError` for invalid titles, and
        :class:`StorageError` when the store is down.
        """
        if not self.page_suffix:
            raise SelfServiceUnavailableError
        page = self._compose_page(title)
        await self._update(chat_id, log_page=page)
        return page

    async def set_consent(self, chat_id: int, mode: ConsentMode) -> None:
        """Set the chat's consent policy for ``/log``."""
        await self._update(chat_id, consent_mode=mode)

    async def set_repo(self, chat_id: int, repo: str) -> None:
        """Bind the chat to a GitHub repository (``owner/name``)."""
        await self._update(chat_id, repo=repo)

    async def set_events(self, chat_id: int, *, enabled: bool, kinds: frozenset[EventKind]) -> None:
        """Configure the chat's repository-event notifications."""
        await self._update(chat_id, events_enabled=enabled, event_kinds=kinds)

    async def migrate(self, old_chat_id: int, new_chat_id: int) -> None:
        """Carry the profile across a group→supergroup chat-id change."""
        if self.store is not None:
            await self.store.migrate(old_chat_id, new_chat_id)

    async def reset(self, chat_id: int) -> None:
        """Forget the chat's profile entirely (token and cursor included)."""
        store = self._require_store()
        await store.delete(chat_id)

    async def profile_of(self, chat_id: int) -> GroupProfile:
        """Return the stored profile (or an empty one) for /settings display."""
        store = self._require_store()
        return await store.get(chat_id) or GroupProfile(chat_id=chat_id)

    async def _update(self, chat_id: int, **changes: Any) -> None:
        store = self._require_store()
        current = await store.get(chat_id) or GroupProfile(chat_id=chat_id)
        await store.upsert(replace(current, **changes))

    def _require_store(self) -> ProfileStore:
        if self.store is None:
            raise SelfServiceUnavailableError
        return self.store

    def _compose_page(self, base: str) -> str:
        """Validate a base path and append the ``/{page_suffix}`` leaf.

        A base that already ends with the suffix is accepted as-is, so
        re-running /setpage with the full page is idempotent.
        """
        normalized = " ".join(base.replace("_", " ").split())
        leaf = f"/{self.page_suffix}"
        page = normalized if normalized.endswith(leaf) else f"{normalized}{leaf}"
        if (
            not normalized
            or normalized.startswith("/")
            or normalized.endswith("/")
            or len(page) > 255  # noqa: PLR2004 -- MediaWiki's title limit
            or any(char in _FORBIDDEN_TITLE_CHARS for char in page)
        ):
            raise PageNotAllowedError
        return page
