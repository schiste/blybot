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

The stamped watermarks are persisted *before* any pipeline runs: a
publish followed by a crash must read as "already ran", never as "still
due" — a skipped digest beats a duplicated wiki section. Persisting
first also keeps the write adjacent to the load, so a minutes-long LLM
run cannot overwrite ``/action add``/``remove`` edits made meanwhile
with a stale snapshot.

A stored action with no ``last_run`` (older rows, hand-edited state) is
baselined: stamped now, run at its *next* slot — never a replay, the
same first-contact rule the repo poll cursors follow.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
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
    # Rotates the truncation window across ticks so an overflow defers
    # scopes instead of permanently starving whatever sorts last.
    _offset: int = field(default=0, init=False)

    async def collect(self) -> list[OutboundMessage]:
        """Return the chat messages produced by every due action this tick."""
        try:
            scoped = await self.store.list_scheduled()
        except StorageError:
            return []
        if len(scoped) > self.max_scopes_per_tick:
            log_event("action_tick", "ignored", skipped=len(scoped) - self.max_scopes_per_tick)
            start = self._offset % len(scoped)
            self._offset = start + self.max_scopes_per_tick
            scoped = (scoped[start:] + scoped[:start])[: self.max_scopes_per_tick]
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
        updated = list(actions)
        due: list[ActionSpec] = []
        for index, spec in enumerate(actions):
            schedule = spec.trigger.schedule
            if spec.trigger.kind is not TriggerKind.SCHEDULE or schedule is None:
                continue
            if spec.last_run is None:  # baseline: next slot, never a replay
                updated[index] = replace(spec, last_run=now)
                continue
            if not schedule.is_due(now, spec.last_run):
                continue
            updated[index] = replace(spec, last_run=now)
            due.append(spec)  # the *old* last_run feeds since_last_run windows
        if tuple(updated) != actions:
            # Persist the stamps before any side effects (module docstring).
            # If this write fails, the per-scope isolation above skips the
            # scope's runs this tick — failing toward "no duplicate publish".
            await self.store.set_actions(scope.chat_id, scope.thread_id, tuple(updated))
        messages: list[OutboundMessage] = []
        for spec in due:
            try:
                messages.extend((await self.engine.run(scope, spec, now)).messages)
            except Exception:
                # Per-action isolation: one failing pipeline (bad page,
                # unknown component, adapter outage) must not block the
                # scope's other due actions. Deliberately broad.
                self.counters.increment("actions_failed")
                log_event("action_run", "error")
        return messages
