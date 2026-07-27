"""ActionScheduler tests: due selection, baselining, isolation, stamping."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta

from blybot.domain.models import ActionContext, ActionScope, OutboundMessage, TriggerKind
from blybot.observability import Counters
from blybot.services.actions import parse_action
from blybot.services.engine import ActionEngine
from blybot.services.policy import GroupPolicy
from blybot.services.schedule import ActionScheduler
from tests.fakes import FakeClock, FakeSink, FakeSource, InMemoryActions, SuffixTransform

REPLY = OutboundMessage(chat_id=-1, thread_id=0, text="published")


def make_scheduler(
    store: InMemoryActions,
    clock: FakeClock,
    sink: FakeSink | None = None,
    transform: SuffixTransform | None = None,
    allowed: set[int] | None = None,
    max_scopes: int = 200,
) -> tuple[ActionScheduler, Counters]:
    counters = Counters()
    engine = ActionEngine(
        sources={"archive_window": FakeSource(payload="hello")},
        transforms={"prompt": transform or SuffixTransform(), "stats": SuffixTransform()},
        sinks={"wiki_section": sink or FakeSink(messages=(REPLY,))},
        counters=counters,
        clock=clock,
    )
    scheduler = ActionScheduler(
        store=store,
        engine=engine,
        groups=GroupPolicy(allowed=allowed if allowed is not None else set()),
        clock=clock,
        counters=counters,
        max_scopes_per_tick=max_scopes,
    )
    return scheduler, counters


async def seed(
    store: InMemoryActions, clock: FakeClock, text: str = "every:6h summarize", chat_id: int = -1
) -> None:
    spec = parse_action(text, now_iso=clock.now().isoformat())
    existing = store.actions.get((chat_id, 0), ())
    store.actions[chat_id, 0] = (*existing, spec)


async def test_due_action_runs_and_stamps_last_run() -> None:
    store, clock = InMemoryActions(), FakeClock()
    await seed(store, clock)
    clock.advance(timedelta(hours=6))
    scheduler, counters = make_scheduler(store, clock)

    assert await scheduler.collect() == [REPLY]
    (stored,) = store.actions[-1, 0]
    assert stored.last_run == clock.now()
    assert counters.snapshot()["actions_run"] == 1
    # The very next tick owes nothing: last_run was just stamped.
    assert await scheduler.collect() == []


async def test_not_yet_due_action_is_left_untouched() -> None:
    store, clock = InMemoryActions(), FakeClock()
    await seed(store, clock)
    clock.advance(timedelta(hours=1))
    scheduler, _counters = make_scheduler(store, clock)
    before = store.actions[-1, 0]

    assert await scheduler.collect() == []
    assert store.actions[-1, 0] == before  # no rewrite when nothing changed


async def test_unstamped_action_is_baselined_not_replayed() -> None:
    store, clock = InMemoryActions(), FakeClock()
    await seed(store, clock)
    (spec,) = store.actions[-1, 0]
    store.actions[-1, 0] = (replace(spec, last_run=None),)  # e.g. hand-edited row
    clock.advance(timedelta(days=2))  # plenty of missed slots
    scheduler, counters = make_scheduler(store, clock)

    assert await scheduler.collect() == []  # baselined, never replayed
    (stored,) = store.actions[-1, 0]
    assert stored.last_run == clock.now()
    assert "actions_run" not in counters.snapshot()
    # From the baseline on, the schedule fires normally.
    clock.advance(timedelta(hours=6))
    assert await scheduler.collect() == [REPLY]


async def test_command_triggered_actions_are_never_scheduled() -> None:
    store, clock = InMemoryActions(), FakeClock()
    stored = parse_action("every:6h summarize", now_iso=clock.now().isoformat())
    command_trigger = replace(
        stored, trigger=replace(stored.trigger, kind=TriggerKind.COMMAND, command="run")
    )
    store.actions[-1, 0] = (command_trigger,)
    clock.advance(timedelta(days=1))
    scheduler, _counters = make_scheduler(store, clock)

    assert await scheduler.collect() == []


async def test_failing_action_is_isolated_and_waits_for_its_next_slot() -> None:
    store, clock = InMemoryActions(), FakeClock()
    await seed(store, clock, "every:6h summarize")  # uses the (failing) prompt transform
    await seed(store, clock, "every:6h stats")  # uses the healthy stats transform
    clock.advance(timedelta(hours=6))
    scheduler, counters = make_scheduler(store, clock, transform=SuffixTransform(fail=True))

    messages = await scheduler.collect()

    assert messages == [REPLY]  # the healthy action still delivered
    assert counters.snapshot()["actions_failed"] == 1
    first, second = store.actions[-1, 0]
    assert first.last_run == clock.now()  # stamped despite the failure:
    assert second.last_run == clock.now()  # retry at the next slot, not every tick


async def test_disallowed_groups_are_skipped() -> None:
    store, clock = InMemoryActions(), FakeClock()
    await seed(store, clock, chat_id=-99)
    clock.advance(timedelta(hours=6))
    scheduler, _counters = make_scheduler(store, clock, allowed={-1})

    assert await scheduler.collect() == []
    (stored,) = store.actions[-99, 0]
    assert stored.last_run is not None
    assert stored.last_run < clock.now()  # untouched, not even stamped


async def test_storage_outage_yields_an_empty_tick() -> None:
    store, clock = InMemoryActions(fail=True), FakeClock()
    scheduler, counters = make_scheduler(store, clock)

    assert await scheduler.collect() == []
    assert counters.snapshot()["action_ticks_failed"] == 1  # not a silent empty tick


async def test_broken_scope_never_blocks_other_scopes() -> None:
    store, clock = InMemoryActions(), FakeClock()
    await seed(store, clock, chat_id=-1)
    await seed(store, clock, chat_id=-2)
    clock.advance(timedelta(hours=6))
    store.fail_writes_for = {(-1, 0)}  # scope -1's last_run write explodes
    scheduler, counters = make_scheduler(store, clock)

    # Scope -1's tick is lost to its storage error; scope -2 still runs.
    assert await scheduler.collect() == [REPLY]
    (stored,) = store.actions[-2, 0]
    assert stored.last_run == clock.now()
    assert counters.snapshot()["action_ticks_failed"] == 1  # the lost scope is counted


async def test_scope_overflow_is_truncated_per_tick() -> None:
    store, clock = InMemoryActions(), FakeClock()
    await seed(store, clock, chat_id=-1)
    await seed(store, clock, chat_id=-2)
    clock.advance(timedelta(hours=6))
    scheduler, _counters = make_scheduler(store, clock, max_scopes=1)

    assert await scheduler.collect() == [REPLY]


async def test_capped_ticks_rotate_so_no_scope_is_permanently_starved() -> None:
    store, clock = InMemoryActions(), FakeClock()
    await seed(store, clock, chat_id=-1)
    await seed(store, clock, chat_id=-2)
    clock.advance(timedelta(hours=6))
    scheduler, _counters = make_scheduler(store, clock, max_scopes=1)

    assert await scheduler.collect() == [REPLY]  # one scope this tick...
    assert await scheduler.collect() == [REPLY]  # ...the other on the next
    for chat_id in (-1, -2):
        (stored,) = store.actions[chat_id, 0]
        assert stored.last_run == clock.now()  # both eventually served


def test_jitter_is_stable_bounded_and_off_for_every() -> None:
    store, clock = InMemoryActions(), FakeClock()
    scheduler, _counters = make_scheduler(store, clock)
    daily = parse_action("daily@06:00 summarize", now_iso=clock.now().isoformat())
    every = parse_action("every:6h summarize", now_iso=clock.now().isoformat())
    dsched, esched = daily.trigger.schedule, every.trigger.schedule
    assert dsched is not None
    assert esched is not None
    scope = ActionScope(chat_id=-1)

    first = scheduler._jitter(scope, daily, dsched)
    assert first == scheduler._jitter(scope, daily, dsched)  # stable across restarts
    assert timedelta(0) <= first < scheduler.jitter_window  # bounded to the window
    assert scheduler._jitter(scope, every, esched) == timedelta(0)  # every: never jittered

    scheduler.jitter_window = timedelta(0)
    assert scheduler._jitter(scope, daily, dsched) == timedelta(0)  # window off


async def test_a_daily_slot_waits_for_its_jitter_before_firing() -> None:
    store, clock = InMemoryActions(), FakeClock()
    spec = replace(parse_action("daily@06:00 summarize", now_iso=clock.now().isoformat()))
    store.actions[-1, 0] = (spec,)
    scheduler, _counters = make_scheduler(store, clock)
    schedule = spec.trigger.schedule
    assert schedule is not None
    jitter = scheduler._jitter(ActionScope(chat_id=-1), spec, schedule)
    slot = datetime(2026, 7, 11, 6, 0, tzinfo=UTC)

    clock.current = slot + jitter - timedelta(minutes=1)
    assert await scheduler.collect() == []  # slot reached, but the jitter hasn't elapsed

    clock.current = slot + jitter + timedelta(minutes=1)
    assert await scheduler.collect() == [REPLY]  # fires once the jitter has passed


async def test_per_scope_action_cap_defers_excess_due_actions() -> None:
    store, clock = InMemoryActions(), FakeClock()
    for _ in range(3):
        await seed(store, clock, "every:6h summarize")  # three aligned, all due
    clock.advance(timedelta(hours=6))
    scheduler, counters = make_scheduler(store, clock)
    scheduler.max_actions_per_scope = 2

    assert await scheduler.collect() == [REPLY, REPLY]  # only two run this tick
    assert counters.snapshot()["actions_run"] == 2
    # The deferred third was left un-stamped, so it is still due next tick.
    assert await scheduler.collect() == [REPLY]
    assert counters.snapshot()["actions_run"] == 3


@dataclass
class StampProbeSink:
    """Sink that records the *persisted* last_run at delivery time."""

    store: InMemoryActions
    stamped: list[datetime | None] = field(default_factory=list)

    async def deliver(self, context: ActionContext, payload: object) -> tuple[OutboundMessage, ...]:
        del context, payload
        (stored,) = self.store.actions[-1, 0]
        self.stamped.append(stored.last_run)
        return (REPLY,)


async def test_watermark_is_persisted_before_the_pipeline_runs() -> None:
    """A publish followed by a crash must read as "already ran": the
    stamp reaches storage before the pipeline can touch the wiki, so a
    restart skips the slot instead of duplicating the section."""
    store, clock = InMemoryActions(), FakeClock()
    await seed(store, clock)
    clock.advance(timedelta(hours=6))
    probe = StampProbeSink(store=store)
    counters = Counters()
    engine = ActionEngine(
        sources={"archive_window": FakeSource(payload="hello")},
        transforms={"prompt": SuffixTransform()},
        sinks={"wiki_section": probe},
        counters=counters,
        clock=clock,
    )
    scheduler = ActionScheduler(
        store=store,
        engine=engine,
        groups=GroupPolicy(allowed=set()),
        clock=clock,
        counters=counters,
    )

    assert await scheduler.collect() == [REPLY]
    assert probe.stamped == [clock.now()]  # already durable when the sink ran


async def test_failed_stamp_write_skips_the_runs_rather_than_risk_duplicates() -> None:
    store, clock = InMemoryActions(), FakeClock()
    await seed(store, clock)
    clock.advance(timedelta(hours=6))
    store.fail_writes_for = {(-1, 0)}
    sink = FakeSink(messages=(REPLY,))
    scheduler, _counters = make_scheduler(store, clock, sink=sink)

    assert await scheduler.collect() == []
    assert sink.delivered == []  # nothing published without a durable stamp
