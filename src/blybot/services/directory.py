"""Resolves each group's effective settings (self-service, spec v2).

Two-tier resolution: a group's stored :class:`GroupProfile` overrides
the operator's environment defaults, field by field. No profile — or no
profile *store*, the pure v1 deployment — means every group simply gets
the defaults, so single-tenant behavior is the degenerate case, not a
special one.

Reads are failure-tolerant by design: if ToolsDB is down, ``resolve``
returns defaults (``/log`` keeps working); only configuration *writes*
surface :class:`~blybot.domain.ports.StorageError` to the admin.

Wiki safety: self-service page targeting is confined to subpages of the
operator-configured ``page_prefix`` — group admins configure *where
under the bot's area* their log lives, never arbitrary pages the shared
wiki account would then be blamed for.
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


@dataclass(eq=False)
class ChannelDirectory:
    """Per-chat settings: stored profile over environment defaults."""

    store: ProfileStore | None
    default_log_page: str
    default_consent: ConsentMode
    default_repo: str
    page_prefix: str  # "" disables self-service page targeting

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
        if self.store is not None:
            try:
                profile = await self.store.get(chat_id)
            except StorageError:
                profile = None
        if profile is None:
            return ChannelSettings(
                log_page=self.default_log_page,
                consent_mode=self.default_consent,
                repo=self.default_repo,
                has_token=False,
                events_enabled=False,
                event_kinds=frozenset(),
                customized=False,
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

        Returns the normalized title. Raises
        :class:`SelfServiceUnavailableError` when page targeting is off,
        :class:`PageNotAllowedError` for out-of-prefix or invalid titles,
        and :class:`StorageError` when the store is down.
        """
        if not self.page_prefix:
            raise SelfServiceUnavailableError
        normalized = self._validate_title(title)
        await self._update(chat_id, log_page=normalized)
        return normalized

    async def set_consent(self, chat_id: int, mode: ConsentMode) -> None:
        """Set the chat's consent policy for ``/log``."""
        await self._update(chat_id, consent_mode=mode)

    async def set_repo(self, chat_id: int, repo: str) -> None:
        """Bind the chat to a GitHub repository (``owner/name``)."""
        await self._update(chat_id, repo=repo)

    async def set_events(self, chat_id: int, *, enabled: bool, kinds: frozenset[EventKind]) -> None:
        """Configure the chat's repository-event notifications."""
        await self._update(chat_id, events_enabled=enabled, event_kinds=kinds)

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

    def _validate_title(self, title: str) -> str:
        normalized = " ".join(title.replace("_", " ").split())
        if (
            not normalized.startswith(self.page_prefix)
            or len(normalized) <= len(self.page_prefix)
            or len(normalized) > 255  # noqa: PLR2004 -- MediaWiki's title limit
            or any(char in _FORBIDDEN_TITLE_CHARS for char in normalized)
        ):
            raise PageNotAllowedError
        return normalized
