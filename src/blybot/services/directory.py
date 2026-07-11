"""Resolves each (group, topic)'s effective settings (self-service).

Three-tier resolution: a forum topic's stored profile overrides the
group default (thread 0), which overrides the operator's environment
defaults. No profile — or no store, the pure v1 deployment — means the
env defaults, so single-tenant behavior is the degenerate case.

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
from blybot.services.rules import MAX_RULES

if TYPE_CHECKING:
    from blybot.domain.models import Rule
    from blybot.domain.ports import ProfileStore

# Characters MediaWiki forbids in page titles (underscores are handled
# by normalization: they are equivalent to spaces).
_FORBIDDEN_TITLE_CHARS: Final = frozenset('#<>[]|{}"')


class SelfServiceUnavailableError(Exception):
    """Self-service is not enabled on this deployment (no store / no prefix)."""


class PageNotAllowedError(Exception):
    """The requested page is outside the operator's allowed prefix or invalid."""


class TooManyRulesError(Exception):
    """The scope already holds the maximum number of rules."""


@dataclass(frozen=True, slots=True)
class ChannelSettings:
    """The effective, fully-resolved settings for one (group, topic)."""

    log_page: str
    consent_mode: ConsentMode
    repo: str
    has_token: bool
    events_enabled: bool
    event_kinds: frozenset[EventKind]
    # The thread whose profile provided the repo/token binding — the
    # token must be fetched from THIS key, not the calling topic (a
    # topic inheriting the group repo shares the group's token).
    repo_thread_id: int
    # Whether the log page came from an explicit /setpage (topic or
    # group) rather than the operator default — self-service groups
    # publish only when this is True.
    page_explicit: bool
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

    async def resolve(self, chat_id: int, thread_id: int = 0) -> ChannelSettings:
        """Resolve a (group, topic)'s settings; never raises.

        Three tiers: a topic's own profile overrides the group default
        (thread 0) overrides the operator env defaults. The page falls
        back per-field; the repo/token/events resolve as a *unit* from
        the first tier that binds a repo (so a topic's token is used
        only for the topic's own repo); consent is group-level only.
        A storage outage degrades to env defaults so /log keeps working.
        """
        group: GroupProfile | None = None
        topic: GroupProfile | None = None
        degraded = False
        if self.store is not None:
            try:
                group = await self.store.get(chat_id, 0)
                topic = await self.store.get(chat_id, thread_id) if thread_id else group
            except StorageError:
                degraded = True

        binder = topic if (topic and topic.repo) else group if (group and group.repo) else None
        explicit_page = (topic.log_page if topic else None) or (group.log_page if group else None)
        return ChannelSettings(
            log_page=explicit_page or self.default_log_page,
            page_explicit=bool(explicit_page),
            consent_mode=(group.consent_mode if group else None) or self.default_consent,
            repo=binder.repo if binder and binder.repo else self.default_repo,
            has_token=bool(binder and binder.has_token),
            events_enabled=bool(binder and binder.events_enabled),
            event_kinds=binder.event_kinds if binder else frozenset(),
            repo_thread_id=binder.thread_id if binder else 0,
            customized=bool((topic and topic is not group) or group),
            degraded=degraded,
        )

    async def set_log_page(self, chat_id: int, thread_id: int, title: str) -> str:
        """Point this (group, topic)'s ``/log`` at a page.

        ``title`` is a base page path; the log lands on its
        ``/{page_suffix}`` subpage. Returns the full page title. Raises
        :class:`SelfServiceUnavailableError` when page targeting is off,
        :class:`PageNotAllowedError` for invalid titles, and
        :class:`StorageError` when the store is down.
        """
        if not self.page_suffix:
            raise SelfServiceUnavailableError
        page = self._compose_page(title)
        await self._update(chat_id, thread_id, log_page=page)
        return page

    async def set_consent(self, chat_id: int, mode: ConsentMode) -> None:
        """Set the group's consent policy for ``/log`` (group-wide, thread 0)."""
        await self._update(chat_id, 0, consent_mode=mode)

    async def set_repo(self, chat_id: int, thread_id: int, repo: str) -> None:
        """Bind this (group, topic) to a GitHub repository (``owner/name``)."""
        await self._update(chat_id, thread_id, repo=repo)

    async def set_events(
        self, chat_id: int, thread_id: int, *, enabled: bool, kinds: frozenset[EventKind]
    ) -> None:
        """Configure this (group, topic)'s repository-event notifications."""
        await self._update(chat_id, thread_id, events_enabled=enabled, event_kinds=kinds)

    async def add_rule(self, chat_id: int, thread_id: int, rule: Rule) -> tuple[Rule, ...]:
        """Append a composable event rule to this (group, topic).

        Raises :class:`TooManyRulesError` at the per-scope cap so a
        runaway ruleset can't bloat one row. Returns the new ruleset.
        """
        store = self._require_store()
        current = await self._profile(store, chat_id, thread_id)
        if len(current.rules) >= MAX_RULES:
            raise TooManyRulesError
        rules = (*current.rules, rule)
        await store.upsert(replace(current, rules=rules))
        return rules

    async def remove_rule(self, chat_id: int, thread_id: int, rule_id: str) -> bool:
        """Drop the rule with ``rule_id``; ``False`` if no such rule exists."""
        store = self._require_store()
        current = await store.get(chat_id, thread_id)
        if current is None:
            return False
        kept = tuple(rule for rule in current.rules if rule.rule_id != rule_id)
        if len(kept) == len(current.rules):
            return False
        await store.upsert(replace(current, rules=kept))
        return True

    async def clear_rules(self, chat_id: int, thread_id: int) -> int:
        """Remove every rule at this scope; returns how many were removed."""
        store = self._require_store()
        current = await store.get(chat_id, thread_id)
        if current is None or not current.rules:
            return 0
        await store.upsert(replace(current, rules=()))
        return len(current.rules)

    async def list_rules(self, chat_id: int, thread_id: int) -> tuple[Rule, ...]:
        """Return this (group, topic)'s stored rules (empty tuple if none)."""
        return (await self.profile_of(chat_id, thread_id)).rules

    async def migrate(self, old_chat_id: int, new_chat_id: int) -> None:
        """Carry every topic's profile across a group→supergroup chat-id change."""
        if self.store is not None:
            await self.store.migrate(old_chat_id, new_chat_id)

    async def reset(self, chat_id: int, thread_id: int) -> None:
        """Forget this (group, topic)'s profile (token and cursor included)."""
        store = self._require_store()
        await store.delete(chat_id, thread_id)

    async def profile_of(self, chat_id: int, thread_id: int) -> GroupProfile:
        """Return the stored profile (or an empty one) for /settings display."""
        return await self._profile(self._require_store(), chat_id, thread_id)

    async def _update(self, chat_id: int, thread_id: int, **changes: Any) -> None:
        store = self._require_store()
        current = await self._profile(store, chat_id, thread_id)
        await store.upsert(replace(current, **changes))

    @staticmethod
    async def _profile(store: ProfileStore, chat_id: int, thread_id: int) -> GroupProfile:
        """The stored profile for a scope, or a fresh empty one."""
        return await store.get(chat_id, thread_id) or GroupProfile(
            chat_id=chat_id, thread_id=thread_id
        )

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
