"""ActionEngine tests: pipeline execution, quiet no-ops, unknown components."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime

import pytest

from blybot.domain.models import (
    ActionContext,
    ActionSpec,
    OutboundMessage,
    Scope,
    StepSpec,
)
from blybot.domain.ports import ActionError
from blybot.observability import Counters
from blybot.services.actions import parse_action
from blybot.services.engine import ActionEngine
from tests.fakes import FakeClock, FakeSink, FakeSource, SuffixTransform

NOW = datetime(2026, 7, 25, 12, 0, tzinfo=UTC)
SCOPE = Scope("telegram", "-1")


def make_engine(
    source: FakeSource | None = None,
    transform: SuffixTransform | None = None,
    sink: FakeSink | None = None,
) -> tuple[ActionEngine, Counters]:
    counters = Counters()
    engine = ActionEngine(
        sources={"archive_window": source or FakeSource(payload="hello")},
        transforms={"prompt": transform or SuffixTransform(), "stats": SuffixTransform(tag="+s")},
        sinks={"wiki_section": sink or FakeSink()},
        counters=counters,
        clock=FakeClock(),
    )
    return engine, counters


def spec_for(text: str = "daily@06:00 summarize") -> ActionSpec:
    return parse_action(text, now_iso=NOW.isoformat())


async def test_engine_threads_payload_through_the_chain_to_the_sink() -> None:
    sink = FakeSink(messages=(OutboundMessage(scope=Scope("telegram", "-1"), text="done"),))
    transform = SuffixTransform()
    engine, counters = make_engine(transform=transform, sink=sink)

    outcome = await engine.run(SCOPE, spec_for("daily@06:00 summarize model=large"), NOW)

    assert outcome.messages == sink.messages
    ((step, payload),) = transform.applied
    assert payload == "hello"
    assert step.param("model") == "large"  # each occurrence sees its own params
    ((context, delivered),) = sink.delivered
    assert delivered == "hello+t"
    assert context.scope == SCOPE
    assert context.now == NOW
    assert counters.snapshot()["actions_run"] == 1


async def test_empty_source_ends_the_run_quietly() -> None:
    sink = FakeSink()
    engine, counters = make_engine(source=FakeSource(payload=None), sink=sink)

    outcome = await engine.run(SCOPE, spec_for(), NOW)
    assert outcome.messages == ()
    assert outcome.payload is None
    assert sink.delivered == []
    assert counters.snapshot() == {"actions_empty": 1}


async def test_dropping_transform_ends_the_run_quietly() -> None:
    sink = FakeSink()
    engine, counters = make_engine(transform=SuffixTransform(drop=True), sink=sink)

    outcome = await engine.run(SCOPE, spec_for(), NOW)
    assert outcome.messages == ()
    assert outcome.payload is None
    assert sink.delivered == []
    assert counters.snapshot() == {"actions_empty": 1}


async def test_unknown_source_raises_a_named_action_error() -> None:
    engine = ActionEngine(
        sources={}, transforms={}, sinks={}, counters=Counters(), clock=FakeClock()
    )
    with pytest.raises(ActionError, match="unknown source 'archive_window'"):
        await engine.run(SCOPE, spec_for(), NOW)


async def test_unknown_transform_and_sink_name_the_missing_component() -> None:
    source = FakeSource(payload="hello")
    engine = ActionEngine(
        sources={"archive_window": source},
        transforms={},
        sinks={},
        counters=Counters(),
        clock=FakeClock(),
    )
    with pytest.raises(ActionError, match="unknown transform 'prompt'"):
        await engine.run(SCOPE, spec_for(), NOW)
    assert source.fetched == []  # still resolved eagerly, no I/O happened

    engine = ActionEngine(
        sources={"archive_window": source},
        transforms={"prompt": SuffixTransform()},
        sinks={},
        counters=Counters(),
        clock=FakeClock(),
    )
    with pytest.raises(ActionError, match="unknown sink 'wiki_section'"):
        await engine.run(SCOPE, spec_for(), NOW)
    assert source.fetched == []


async def test_chain_order_is_the_spec_order() -> None:
    spec = ActionSpec(
        action_id="ab12",
        trigger=spec_for().trigger,
        source=StepSpec(name="archive_window"),
        transforms=(
            StepSpec(name="prompt", params=(("tag", "+1"),)),
            StepSpec(name="prompt", params=(("tag", "+2"),)),
        ),
        sinks=(StepSpec(name="wiki_section"),),
    )
    sink = FakeSink()
    engine, _counters = make_engine(sink=sink)

    await engine.run(SCOPE, spec, NOW)

    ((_context, delivered),) = sink.delivered
    assert delivered == "hello+1+2"


async def test_caller_provided_payloads_replace_the_source() -> None:
    """The phase-5 seam: interactive flows inject their payload directly."""
    sink = FakeSink(messages=(OutboundMessage(scope=Scope("telegram", "-1"), text="done"),))
    source = FakeSource(payload="from the source")
    engine, counters = make_engine(source=source, sink=sink)

    outcome = await engine.run(SCOPE, spec_for(), NOW, payload="provided by the caller")

    assert outcome.messages == sink.messages
    assert outcome.payload == "provided by the caller+t"  # the sink's input, readable
    assert source.fetched == []  # the source never ran
    ((_context, delivered),) = sink.delivered
    assert delivered == "provided by the caller+t"
    assert counters.snapshot()["actions_run"] == 1


async def test_provided_payloads_skip_source_resolution_entirely() -> None:
    engine = ActionEngine(
        sources={},  # no source registered at all
        transforms={"prompt": SuffixTransform(), "stats": SuffixTransform()},
        sinks={"wiki_section": FakeSink()},
        counters=Counters(),
        clock=FakeClock(),
    )
    # Would raise "unknown source" without a provided payload; with one it runs.
    await engine.run(SCOPE, spec_for(), NOW, payload="direct")


async def test_every_sink_receives_the_payload_and_their_messages_concatenate() -> None:
    """One summary, several destinations: publish AND fan out (#70)."""
    wiki = FakeSink(messages=(OutboundMessage(scope=SCOPE, text="published"),))
    subscribers = FakeSink(
        messages=(
            OutboundMessage(scope=Scope("telegram", "11"), text="digest"),
            OutboundMessage(scope=Scope("telegram", "22"), text="digest"),
        )
    )
    spec = replace(
        spec_for(),
        sinks=(StepSpec(name="wiki_section"), StepSpec(name="subscriber_dm")),
    )
    engine = ActionEngine(
        sources={"archive_window": FakeSource(payload="hello")},
        transforms={"prompt": SuffixTransform()},
        sinks={"wiki_section": wiki, "subscriber_dm": subscribers},
        counters=Counters(),
        clock=FakeClock(),
    )

    outcome = await engine.run(SCOPE, spec, NOW)

    # Both sinks saw the same final payload...
    assert [payload for _context, payload in wiki.delivered] == ["hello+t"]
    assert [payload for _context, payload in subscribers.delivered] == ["hello+t"]
    # ...and their messages arrive in spec order.
    assert [message.text for message in outcome.messages] == ["published", "digest", "digest"]


async def test_a_sink_is_handed_its_own_step_not_the_specs() -> None:
    """With several sinks, `context.spec` cannot say which params are mine."""
    seen: list[StepSpec] = []

    class _StepRecordingSink:
        async def deliver(
            self, context: ActionContext, step: StepSpec, payload: object
        ) -> tuple[OutboundMessage, ...]:
            del context, payload
            seen.append(step)
            return ()

    spec = replace(
        spec_for(),
        sinks=(
            StepSpec(name="wiki_section", params=(("page", "Meta:One"),)),
            StepSpec(name="subscriber_dm", params=(("page", "Meta:Two"),)),
        ),
    )
    engine = ActionEngine(
        sources={"archive_window": FakeSource(payload="hello")},
        transforms={"prompt": SuffixTransform()},
        sinks={"wiki_section": _StepRecordingSink(), "subscriber_dm": _StepRecordingSink()},
        counters=Counters(),
        clock=FakeClock(),
    )

    await engine.run(SCOPE, spec, NOW)

    assert [step.param("page") for step in seen] == ["Meta:One", "Meta:Two"]
