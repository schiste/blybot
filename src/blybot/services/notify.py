"""Repository-event digests for opted-in groups (spec v2, Phase C).

Polls each event-enabled group's bound repository (no webhooks — the
bot stays outbound-only), reduces new happenings to at most one digest
message per group per cycle, and advances the group's cursor. Errors
are isolated per group: one broken repo, missing token, or storage
hiccup never blocks the other groups' digests.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Final

from blybot.domain.ports import IssueTrackerError, StorageError
from blybot.observability import Counters, log_event

if TYPE_CHECKING:
    from blybot.domain.models import GroupProfile, RepoEvent
    from blybot.domain.ports import ProfileStore, RepoGateway, TokenVault
    from blybot.services.policy import GroupPolicy

_DIGEST_LINES: Final = 5


@dataclass(eq=False)
class RepoNotifier:
    """Collects per-group digests of fresh repository events."""

    store: ProfileStore
    vault: TokenVault
    gateway: RepoGateway
    groups: GroupPolicy
    counters: Counters
    max_groups_per_tick: int = 200

    async def collect(self) -> list[tuple[int, str]]:
        """Return ``(chat_id, digest)`` pairs for groups with fresh events."""
        try:
            profiles = await self.store.list_event_enabled()
        except StorageError:
            return []
        if len(profiles) > self.max_groups_per_tick:
            log_event("repo_poll", "ignored", skipped=len(profiles) - self.max_groups_per_tick)
            profiles = profiles[: self.max_groups_per_tick]
        digests: list[tuple[int, str]] = []
        for profile in profiles:
            if not self.groups.is_allowed(profile.chat_id):
                continue  # never push into groups the operator excluded
            digest = await self._digest_for(profile)
            if digest is not None:
                digests.append((profile.chat_id, digest))
        return digests

    async def _digest_for(self, profile: GroupProfile) -> str | None:
        if not profile.repo:
            return None
        try:
            token = await self.vault.fetch_token(profile.chat_id)
            if not token:
                return None
            cursor = await self.store.get_cursor(profile.chat_id)
            events, new_cursor = await self.gateway.events_since(profile.repo, token, cursor)
            if new_cursor and new_cursor != cursor:
                await self.store.set_cursor(profile.chat_id, new_cursor, profile.repo)
        except (StorageError, IssueTrackerError):
            log_event("repo_poll", "error")
            return None
        wanted = [event for event in events if event.kind in profile.event_kinds]
        if not wanted:
            return None
        self.counters.increment("repo_digests")
        log_event("repo_poll", "ok", events=len(wanted))
        return _format_digest(profile.repo, wanted)


def _format_digest(repo: str, events: list[RepoEvent]) -> str:
    lines = [f"{repo}:"]
    lines += [f"- {event.title} {event.url}".rstrip() for event in events[:_DIGEST_LINES]]
    if len(events) > _DIGEST_LINES:
        lines.append(f"…and {len(events) - _DIGEST_LINES} more")
    return "\n".join(lines)
