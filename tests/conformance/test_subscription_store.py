"""SubscriptionStore contract — every implementation proves these semantics.

Runs against BOTH the in-memory fake and the real ToolsDbSubscriptions (over
its SQL-level fake). Owner-scoping, watermark advance, and re-keying are the
port's meaning; an impl that drifts fails here.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from blybot.domain.models import Schedule, Scope
from blybot.domain.ports import SubscriptionStore
from blybot.domain.subscriptions import Subscription
from tests.conformance._impls import SUBSCRIPTION_STORES

NOW = datetime(2026, 7, 25, 8, 0, tzinfo=UTC)


@pytest.fixture(
    params=[build for _, build in SUBSCRIPTION_STORES],
    ids=[name for name, _ in SUBSCRIPTION_STORES],
)
def subscriptions(request: pytest.FixtureRequest) -> SubscriptionStore:
    build = request.param
    store: SubscriptionStore = build()
    return store


def _sub(
    sub_id: str = "s1", dm: int = 500, chat: int = -100, last_run: datetime | None = None
) -> Subscription:
    return Subscription(
        sub_id=sub_id,
        dm=Scope("telegram", str(dm)),
        scope=Scope("telegram", str(chat)),
        schedule=Schedule(kind="daily", hour=8),
        recipe="summarize",
        lang="en",
        last_run=last_run,
    )


async def test_add_then_list_for_user_is_owner_scoped(subscriptions: SubscriptionStore) -> None:
    await subscriptions.add(_sub("s1", dm=500))
    await subscriptions.add(_sub("s2", dm=500))
    await subscriptions.add(_sub("s3", dm=999))  # a different owner

    mine = await subscriptions.list_for_user(Scope("telegram", "500"))
    assert [s.sub_id for s in mine] == ["s1", "s2"]  # only this DM's, oldest first
    assert {s.sub_id for s in await subscriptions.list_all()} == {"s1", "s2", "s3"}


async def test_remove_is_owner_scoped(subscriptions: SubscriptionStore) -> None:
    await subscriptions.add(_sub("s1", dm=500))

    assert await subscriptions.remove(Scope("telegram", "999"), "s1") is False  # not the owner
    assert [s.sub_id for s in await subscriptions.list_all()] == ["s1"]  # nothing removed
    assert await subscriptions.remove(Scope("telegram", "500"), "s1") is True  # owner removes
    assert await subscriptions.list_all() == []


async def test_stamp_advances_last_run(subscriptions: SubscriptionStore) -> None:
    await subscriptions.add(_sub("s1", dm=500))
    await subscriptions.stamp("s1", NOW)

    (stored,) = await subscriptions.list_all()
    assert stored.last_run == NOW


async def test_migrate_rekeys_the_source_scope(subscriptions: SubscriptionStore) -> None:
    await subscriptions.add(_sub("s1", chat=-100))
    await subscriptions.add(_sub("s2", chat=-200))  # a bystander group, untouched

    await subscriptions.migrate(Scope("telegram", "-100"), Scope("telegram", "-999"))

    by_id = {s.sub_id: int(s.scope.channel) for s in await subscriptions.list_all()}
    assert by_id == {"s1": -999, "s2": -200}
