"""Rule-driven repository notifications for opted-in groups (spec R-rules).

Re-homed onto the action framework (v3 phase 5): the poll is a
``repo_events`` source, matching + formatting is a ``rule_match``
transform, and delivery is the generic ``telegram_message`` sink. The
:class:`RepoNotifier` shell keeps what the framework does not own — the
events-enabled scope listing, the operator allowlist, the per-tick cap,
and per-scope error isolation — and drives one constant spec through the
engine per scope. Rules stay rules (``rules_json`` and the ``/rule``
UX are untouched): they are *filters* the transform consults, so no
stored-data migration was needed.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Final

from blybot.domain.models import (
    ActionScope,
    ActionSpec,
    DeliveryMode,
    OutboundMessage,
    RepoEventsBatch,
    StepSpec,
    TriggerKind,
    TriggerSpec,
)
from blybot.domain.ports import ActionError, StorageError
from blybot.observability import log_event
from blybot.services.rules import format_event, resources_for

if TYPE_CHECKING:
    from blybot.domain.models import ActionContext, GroupProfile, RepoEvent
    from blybot.domain.ports import ProfileStore, RepoPoller, TokenVault
    from blybot.observability import Counters
    from blybot.services.engine import ActionEngine
    from blybot.services.policy import GroupPolicy

_DIGEST_LINES: Final = 5
_LIVE_CAP: Final = 10  # most individual live messages one scope gets per cycle

# The notification pipeline every events-enabled scope runs per tick.
REPO_DIGEST_ACTION: Final = ActionSpec(
    action_id="repo",
    trigger=TriggerSpec(kind=TriggerKind.COMMAND, command="repo_poll"),
    source=StepSpec(name="repo_events"),
    transforms=(StepSpec(name="rule_match"),),
    sink=StepSpec(name="telegram_message"),
)


@dataclass(eq=False)
class RepoEventsSource:
    """Polls the scope's bound repo streams; emits this cycle's fresh events.

    Owns everything stateful about the poll: profile + token lookup and
    the per-resource cursor watermarks (advanced under the repo guard).
    Returns ``None`` — ending the run quietly — for scopes with nothing
    bound or nothing new.
    """

    store: ProfileStore
    vault: TokenVault
    gateway: RepoPoller

    async def fetch(self, context: ActionContext) -> RepoEventsBatch | None:
        """Return the scope's fresh events with the rules to match them against."""
        profile = await self.store.get(context.scope.chat_id, context.scope.thread_id)
        if profile is None or not profile.repo or not profile.rules:
            return None
        token = await self.vault.fetch_token(profile.chat_id, profile.thread_id)
        if not token:
            return None
        events = await self._poll(profile, profile.repo, token)
        if not events:
            return None
        return RepoEventsBatch(repo=profile.repo, rules=profile.rules, events=tuple(events))

    async def _poll(self, profile: GroupProfile, repo: str, token: str) -> list[RepoEvent]:
        cursors = await self.store.get_cursors(profile.chat_id, profile.thread_id)
        collected: list[RepoEvent] = []
        changed = False
        for resource in sorted(resources_for(profile.rules), key=lambda item: item.value):
            previous = cursors.get(resource.value)
            events, new_cursor = await self.gateway.poll_resource(repo, token, resource, previous)
            collected.extend(events)
            if new_cursor and new_cursor != previous:
                cursors[resource.value] = new_cursor
                changed = True
        if changed:
            await self.store.set_cursors(profile.chat_id, profile.thread_id, cursors, repo)
        return collected


@dataclass(eq=False)
class RuleMatchTransform:
    """Matches events against the scope's rules; renders the chat lines.

    Pure matching and formatting: one line per **live** match (capped,
    with an overflow summary) plus one combined **digest** block. An
    event matching both modes is delivered once in each.
    """

    counters: Counters

    async def apply(
        self, context: ActionContext, step: StepSpec, payload: object
    ) -> tuple[str, ...] | None:
        """Return the cycle's chat lines, or ``None`` when nothing matched."""
        del context, step
        if not isinstance(payload, RepoEventsBatch):
            msg = "rule matching needs a repo events batch to work on"
            raise ActionError(msg)
        live: list[str] = []
        digest: list[str] = []
        for event in payload.events:
            modes = {rule.mode for rule in payload.rules if rule.matches(event)}
            if not modes:
                continue
            line = format_event(event)
            if DeliveryMode.LIVE in modes:
                live.append(line)
            if DeliveryMode.DIGEST in modes:
                digest.append(line)
        if not live and not digest:
            return None
        self.counters.increment("repo_digests")
        log_event("repo_poll", "ok", events=len(live) + len(digest))
        lines = [f"{payload.repo} — {line}" for line in live[:_LIVE_CAP]]
        if len(live) > _LIVE_CAP:
            lines.append(f"{payload.repo}: …and {len(live) - _LIVE_CAP} more live events")
        if digest:
            lines.append(_format_digest(payload.repo, digest))
        return tuple(lines)


@dataclass(eq=False)
class ChatMessagesSink:
    """The generic ``telegram_message`` sink: one message per payload line."""

    async def deliver(self, context: ActionContext, payload: object) -> tuple[OutboundMessage, ...]:
        """Wrap each line in an :class:`OutboundMessage` for the run's scope."""
        if not (isinstance(payload, tuple) and all(isinstance(line, str) for line in payload)):
            msg = "the chat sink needs a tuple of message lines to send"
            raise ActionError(msg)
        return tuple(
            OutboundMessage(
                chat_id=context.scope.chat_id, thread_id=context.scope.thread_id, text=line
            )
            for line in payload
        )


@dataclass(eq=False)
class RepoNotifier:
    """Runs the notification pipeline for every events-enabled scope."""

    store: ProfileStore
    groups: GroupPolicy
    engine: ActionEngine
    max_groups_per_tick: int = 200
    # Rotates the truncation window across ticks so an overflow defers
    # scopes instead of permanently starving whatever sorts last.
    _offset: int = field(default=0, init=False)

    async def collect(self) -> list[OutboundMessage]:
        """Return the chat messages for this cycle's matched events."""
        try:
            profiles = await self.store.list_event_enabled()
        except StorageError:
            return []
        if len(profiles) > self.max_groups_per_tick:
            log_event("repo_poll", "ignored", skipped=len(profiles) - self.max_groups_per_tick)
            start = self._offset % len(profiles)
            self._offset = start + self.max_groups_per_tick
            profiles = (profiles[start:] + profiles[:start])[: self.max_groups_per_tick]
        messages: list[OutboundMessage] = []
        for profile in profiles:
            if not self.groups.is_allowed(profile.chat_id):
                continue  # never push into groups the operator excluded
            scope = ActionScope(chat_id=profile.chat_id, thread_id=profile.thread_id)
            try:
                outcome = await self.engine.run(scope, REPO_DIGEST_ACTION)
                messages.extend(outcome.messages)
            except Exception:
                # Per-scope isolation boundary: a broken repo, missing
                # token, storage hiccup, or even a bad stored regex in one
                # scope's rules must not abort the whole tick (docstring
                # contract). Deliberately broad.
                log_event("repo_poll", "error")
        return messages


def _format_digest(repo: str, lines: list[str]) -> str:
    body = [f"{repo}:"]
    body += [f"- {line}" for line in lines[:_DIGEST_LINES]]
    if len(lines) > _DIGEST_LINES:
        body.append(f"…and {len(lines) - _DIGEST_LINES} more")
    return "\n".join(body)
