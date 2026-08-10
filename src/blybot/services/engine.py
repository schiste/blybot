"""The action engine: runs one configured pipeline end to end.

Given an :class:`~blybot.domain.models.ActionSpec` and registries of
named components, the engine resolves every step up front (fail fast,
before any I/O), fetches the source payload, threads it through the
transform chain, and hands the result to the sink. A ``None`` payload at
any point ends the run quietly — an empty archive window is normal, not
an error. The engine knows nothing about Telegram, wikis, or storage;
those live behind the :class:`~blybot.domain.ports.Source` /
:class:`~blybot.domain.ports.Transform` / :class:`~blybot.domain.ports.Sink`
protocols its registries hold.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, TypeVar

from blybot.domain.models import ActionContext, RunOutcome
from blybot.domain.ports import ActionError

if TYPE_CHECKING:
    from collections.abc import Mapping
    from datetime import datetime

    from blybot.domain.models import ActionSpec, OutboundMessage, Scope
    from blybot.domain.ports import Clock, Sink, Source, Transform
    from blybot.observability import Counters

_ComponentT = TypeVar("_ComponentT")


@dataclass(eq=False)
class ActionEngine:
    """Resolves component names and executes action pipelines."""

    sources: Mapping[str, Source]
    transforms: Mapping[str, Transform]
    sinks: Mapping[str, Sink]
    counters: Counters
    clock: Clock

    async def run(
        self,
        scope: Scope,
        spec: ActionSpec,
        now: datetime | None = None,
        payload: object | None = None,
    ) -> RunOutcome:
        """Run one action; return the final payload plus any chat messages.

        A caller-provided ``payload`` replaces the source step entirely —
        the seam interactive flows (a replied-to message, a DM) use to
        enter the pipeline, since their payloads exist before any spec
        runs. Raises :class:`ActionError` when the spec names a component
        this deployment does not register — a configuration problem the
        caller may show to a group admin.
        """
        source = (
            None if payload is not None else self._resolve(self.sources, spec.source.name, "source")
        )
        chain = [
            (step, self._resolve(self.transforms, step.name, "transform"))
            for step in spec.transforms
        ]
        sink_chain = [(step, self._resolve(self.sinks, step.name, "sink")) for step in spec.sinks]

        context = ActionContext(scope=scope, spec=spec, now=now or self.clock.now())
        if source is not None:
            payload = await source.fetch(context)
        for step, transform in chain:
            if payload is None:
                break
            payload = await transform.apply(context, step, payload)
        if payload is None:
            self.counters.increment("actions_empty")
            return RunOutcome(payload=None)
        messages: list[OutboundMessage] = []
        for step, sink in sink_chain:
            messages.extend(await sink.deliver(context, step, payload))
        self.counters.increment("actions_run")
        return RunOutcome(payload=payload, messages=tuple(messages))

    @staticmethod
    def _resolve(registry: Mapping[str, _ComponentT], name: str, kind: str) -> _ComponentT:
        component = registry.get(name)
        if component is None:
            msg = f"unknown {kind} {name!r} — this deployment does not provide it"
            raise ActionError(msg)
        return component
