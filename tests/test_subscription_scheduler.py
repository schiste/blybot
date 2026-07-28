"""SubscriptionScheduler tests: due selection, stamp-before-send, DM re-wrap."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta

from blybot.adapters.telegram.transport import TELEGRAM_CAPABILITIES
from blybot.domain.models import (
    GroupProfile,
    OutboundMessage,
    PlatformCapabilities,
    Schedule,
    Scope,
)
from blybot.domain.subscriptions import Subscription
from blybot.observability import Counters
from blybot.services.engine import ActionEngine
from blybot.services.subscriptions import SubscriptionScheduler
from tests.fakes import (
    FakeClock,
    FakeSink,
    FakeSource,
    InMemoryProfiles,
    InMemorySubscriptions,
    SuffixTransform,
)

NOW = datetime(2026, 7, 25, 12, 0, tzinfo=UTC)
DIGEST = OutboundMessage(scope=Scope("telegram", "-100"), text="digest")


def make(
    subs: InMemorySubscriptions,
    profiles: InMemoryProfiles | None = None,
    capabilities: PlatformCapabilities = TELEGRAM_CAPABILITIES,
) -> tuple[SubscriptionScheduler, Counters]:
    clock = FakeClock(current=NOW)
    counters = Counters()
    engine = ActionEngine(
        sources={"archive_window": FakeSource(payload="hello")},
        transforms={"prompt": SuffixTransform(), "stats": SuffixTransform()},
        sinks={"reply": FakeSink(messages=(DIGEST,))},
        counters=counters,
        clock=clock,
    )
    scheduler = SubscriptionScheduler(
        subscriptions=subs,
        profiles=profiles if profiles is not None else InMemoryProfiles(),
        engine=engine,
        clock=clock,
        counters=counters,
        capabilities=capabilities,
    )
    return scheduler, counters


def a_sub(
    sub_id: str = "s1",
    dm: int = 500,
    chat: int = -100,
    recipe: str = "summarize",
    last_run: datetime | None = None,
) -> Subscription:
    return Subscription(
        sub_id=sub_id,
        dm=Scope("telegram", str(dm)),
        scope=Scope("telegram", str(chat)),
        schedule=Schedule(kind="daily", hour=8),
        recipe=recipe,
        lang="en",
        last_run=last_run,
    )


def subscribable(chat_id: int = -100) -> GroupProfile:
    return GroupProfile(
        scope=Scope("telegram", str(chat_id)), capture_enabled=True, subscribe_code="code"
    )


async def test_unstamped_subscription_is_baselined_not_delivered() -> None:
    subs = InMemorySubscriptions()
    await subs.add(a_sub(last_run=None))
    scheduler, counters = make(subs)

    assert await scheduler.collect() == []
    assert subs.subs["s1"].last_run == NOW  # baselined
    assert "subscription_digests" not in counters.snapshot()


async def test_not_due_subscription_is_left_alone() -> None:
    subs = InMemorySubscriptions()
    await subs.add(a_sub(last_run=NOW - timedelta(hours=1)))  # today's 08:00 slot already ran
    scheduler, _ = make(subs)
    assert await scheduler.collect() == []


async def test_scheduler_skips_when_platform_lacks_durable_dm() -> None:
    subs = InMemorySubscriptions()
    await subs.add(a_sub(dm=500, last_run=NOW - timedelta(days=1)))  # otherwise due
    scheduler, _ = make(subs, capabilities=replace(TELEGRAM_CAPABILITIES, durable_dm=False))
    assert await scheduler.collect() == []
    assert subs.subs["s1"].last_run == NOW - timedelta(days=1)  # untouched, not even stamped


async def test_due_subscription_delivers_a_dm_digest() -> None:
    subs = InMemorySubscriptions()
    await subs.add(a_sub(dm=500, last_run=NOW - timedelta(days=1)))
    profiles = InMemoryProfiles()
    await profiles.upsert(subscribable())
    scheduler, counters = make(subs, profiles)

    messages = await scheduler.collect()

    # re-targeted from the scope (-100) to the subscriber's DM (500):
    assert messages == [OutboundMessage(scope=Scope("telegram", "500"), text="digest")]
    assert subs.subs["s1"].last_run == NOW  # watermark stamped before delivery
    assert counters.snapshot()["subscription_digests"] == 1


async def test_stats_recipe_uses_the_stats_transform() -> None:
    subs = InMemorySubscriptions()
    await subs.add(a_sub(recipe="stats", last_run=NOW - timedelta(days=1)))
    profiles = InMemoryProfiles()
    await profiles.upsert(subscribable())
    scheduler, _ = make(subs, profiles)
    assert len(await scheduler.collect()) == 1


async def test_skips_when_scope_not_subscribable_or_missing() -> None:
    subs = InMemorySubscriptions()
    await subs.add(a_sub("s1", chat=-100, last_run=NOW - timedelta(days=1)))  # code cleared
    await subs.add(a_sub("s2", chat=-200, last_run=NOW - timedelta(days=1)))  # no profile
    profiles = InMemoryProfiles()
    await profiles.upsert(
        GroupProfile(scope=Scope("telegram", "-100"), capture_enabled=True, subscribe_code=None)
    )
    scheduler, counters = make(subs, profiles)

    assert await scheduler.collect() == []
    assert subs.subs["s1"].last_run == NOW - timedelta(days=1)  # dormant, not stamped
    assert "subscription_digests" not in counters.snapshot()


async def test_storage_outage_yields_an_empty_tick() -> None:
    subs = InMemorySubscriptions(fail=True)
    scheduler, counters = make(subs)
    assert await scheduler.collect() == []
    assert counters.snapshot()["subscription_ticks_failed"] == 1


async def test_one_broken_subscription_never_blocks_the_tick() -> None:
    subs = InMemorySubscriptions()
    await subs.add(a_sub("s1", chat=-100, last_run=NOW - timedelta(days=1)))
    await subs.add(a_sub("s2", chat=-100, dm=600, last_run=NOW - timedelta(days=1)))
    scheduler, counters = make(subs, InMemoryProfiles(fail=True))  # get() explodes per sub

    assert await scheduler.collect() == []
    assert counters.snapshot()["subscription_ticks_failed"] == 2  # both isolated and counted
