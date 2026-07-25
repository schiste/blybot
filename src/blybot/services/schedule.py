"""Scheduled action runs for opted-in scopes.

Each tick the scheduler loads every scope with configured actions,
selects the schedule-triggered ones that are due, stamps their
``last_run``, and hands them to the engine. Errors are isolated at two
levels, matching :class:`~blybot.services.notify.RepoNotifier`'s
contract: a broken scope never blocks other scopes, and a failing
action never blocks its scope's other actions. A failed run is not
retried until its next due slot — its ``last_run`` was stamped for the
attempt, so a permanently broken action costs one try per slot, not one
per tick.

A stored action with no ``last_run`` (older rows, hand-edited state) is
baselined: stamped now, run at its *next* slot — never a replay, the
same first-contact rule the repo poll cursors follow.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import TYPE_CHECKING

from blybot.domain.models import TriggerKind
from blybot.domain.ports import StorageError
from blybot.observability import log_event

if TYPE_CHECKING:
    from blybot.domain.models import ActionScope, ActionSpec, OutboundMessage
    from blybot.domain.ports import ActionStore, Clock
    from blybot.observability import Counters
    from blybot.services.engine import ActionEngine
    from blybot.services.policy import GroupPolicy


@dataclass(eq=False)
class ActionScheduler:
    """Runs each scope's due scheduled actions once per tick."""

    store: ActionStore
    engine: ActionEngine
    groups: GroupPolicy
    clock: Clock
    counters: Counters
    max_scopes_per_tick: int = 200

    async def collect(self) -> list[OutboundMessage]:
        """Return the chat messages produced by every due action this tick."""
        try:
            scoped = await self.store.list_scheduled()
        except StorageError:
            return []
        if len(scoped) > self.max_scopes_per_tick:
            log_event("action_tick", "ignored", skipped=len(scoped) - self.max_scopes_per_tick)
            scoped = scoped[: self.max_scopes_per_tick]
        messages: list[OutboundMessage] = []
        for scope, actions in scoped:
            if not self.groups.is_allowed(scope.chat_id):
                continue  # never run pipelines for groups the operator excluded
            try:
                messages.extend(await self._for_scope(scope, actions))
            except Exception:
                # Per-scope isolation boundary (docstring contract):
                # storage hiccups or corrupt stored specs in one scope
                # must not abort the whole tick. Deliberately broad.
                log_event("action_tick", "error")
        return messages

    async def _for_scope(
        self, scope: ActionScope, actions: tuple[ActionSpec, ...]
    ) -> list[OutboundMessage]:
        now = self.clock.now()
        messages: list[OutboundMessage] = []
        updated = list(actions)
        changed = False
        for index, spec in enumerate(actions):
            schedule = spec.trigger.schedule
            if spec.trigger.kind is not TriggerKind.SCHEDULE or schedule is None:
                continue
            if spec.last_run is None:  # baseline: next slot, never a replay
                updated[index] = replace(spec, last_run=now)
                changed = True
                continue
            if not schedule.is_due(now, spec.last_run):
                continue
            updated[index] = replace(spec, last_run=now)
            changed = True
            try:
                messages.extend(await self.engine.run(scope, spec, now))
            except Exception:
                # Per-action isolation: one failing pipeline (bad page,
                # unknown component, adapter outage) must not block the
                # scope's other due actions. Deliberately broad.
                self.counters.increment("actions_failed")
                log_event("action_run", "error")
        if changed:
            await self.store.set_actions(scope.chat_id, scope.thread_id, tuple(updated))
        return messages
