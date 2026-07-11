"""Rule-driven repository notifications for opted-in groups (spec R-rules).

Each event-enabled scope owns a list of composable rules. Every poll
cycle the notifier polls exactly the resource streams those rules need
(never the whole firehose, never a webhook — the bot stays
outbound-only), matches each fresh event against every rule, and emits
one message per **live** match plus one combined **digest** message for
the cycle's digest matches. Per-resource cursors advance under a repo
guard. Errors are isolated per scope: one broken repo, missing token,
or storage hiccup never blocks the other scopes.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Final

from blybot.domain.models import DeliveryMode
from blybot.domain.ports import IssueTrackerError, StorageError
from blybot.observability import log_event
from blybot.services.rules import format_event, resources_for

if TYPE_CHECKING:
    from blybot.domain.models import GroupProfile, RepoEvent
    from blybot.domain.ports import ProfileStore, RepoGateway, TokenVault
    from blybot.observability import Counters
    from blybot.services.policy import GroupPolicy

_DIGEST_LINES: Final = 5
_LIVE_CAP: Final = 10  # most individual live messages one scope gets per cycle


@dataclass(eq=False)
class RepoNotifier:
    """Collects per-scope live messages and digests from matched events."""

    store: ProfileStore
    vault: TokenVault
    gateway: RepoGateway
    groups: GroupPolicy
    counters: Counters
    max_groups_per_tick: int = 200

    async def collect(self) -> list[tuple[int, int, str]]:
        """Return ``(chat_id, thread_id, text)`` messages for matched events."""
        try:
            profiles = await self.store.list_event_enabled()
        except StorageError:
            return []
        if len(profiles) > self.max_groups_per_tick:
            log_event("repo_poll", "ignored", skipped=len(profiles) - self.max_groups_per_tick)
            profiles = profiles[: self.max_groups_per_tick]
        messages: list[tuple[int, int, str]] = []
        for profile in profiles:
            if not self.groups.is_allowed(profile.chat_id):
                continue  # never push into groups the operator excluded
            messages.extend(await self._for_scope(profile))
        return messages

    async def _for_scope(self, profile: GroupProfile) -> list[tuple[int, int, str]]:
        if not profile.repo or not profile.rules:
            return []
        repo = profile.repo
        try:
            token = await self.vault.fetch_token(profile.chat_id, profile.thread_id)
            if not token:
                return []
            events = await self._poll(profile, repo, token)
        except (StorageError, IssueTrackerError):
            log_event("repo_poll", "error")
            return []
        return self._deliver(profile, repo, events)

    async def _poll(self, profile: GroupProfile, repo: str, token: str) -> list[RepoEvent]:
        cursors = await self.store.get_cursors(profile.chat_id, profile.thread_id)
        collected: list[RepoEvent] = []
        changed = False
        for resource in sorted(resources_for(profile.rules), key=lambda item: item.value):
            events, new_cursor = await self.gateway.poll_resource(
                repo, token, resource, cursors.get(resource.value)
            )
            collected.extend(events)
            if new_cursor and new_cursor != cursors.get(resource.value):
                cursors[resource.value] = new_cursor
                changed = True
        if changed:
            await self.store.set_cursors(profile.chat_id, profile.thread_id, cursors, repo)
        return collected

    def _deliver(
        self, profile: GroupProfile, repo: str, events: list[RepoEvent]
    ) -> list[tuple[int, int, str]]:
        live: list[str] = []
        digest: list[str] = []
        for event in events:
            modes = {rule.mode for rule in profile.rules if rule.matches(event)}
            if not modes:
                continue
            line = format_event(event)
            if DeliveryMode.LIVE in modes:
                live.append(line)
            if DeliveryMode.DIGEST in modes:
                digest.append(line)
        if not live and not digest:
            return []
        self.counters.increment("repo_digests")
        log_event("repo_poll", "ok", events=len(live) + len(digest))
        return self._render(profile, repo, live, digest)

    @staticmethod
    def _render(
        profile: GroupProfile, repo: str, live: list[str], digest: list[str]
    ) -> list[tuple[int, int, str]]:
        scope = (profile.chat_id, profile.thread_id)
        messages = [(*scope, f"{repo} — {line}") for line in live[:_LIVE_CAP]]
        if len(live) > _LIVE_CAP:
            messages.append((*scope, f"{repo}: …and {len(live) - _LIVE_CAP} more live events"))
        if digest:
            messages.append((*scope, _format_digest(repo, digest)))
        return messages


def _format_digest(repo: str, lines: list[str]) -> str:
    body = [f"{repo}:"]
    body += [f"- {line}" for line in lines[:_DIGEST_LINES]]
    if len(lines) > _DIGEST_LINES:
        body.append(f"…and {len(lines) - _DIGEST_LINES} more")
    return "\n".join(body)
