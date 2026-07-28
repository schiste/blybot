"""ToolsDbSubscriptions adapter tests (the durable DM-chat-id carve-out store)."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from blybot.adapters.toolsdb.subscriptions import ToolsDbSubscriptions
from blybot.domain.models import Schedule, Scope
from blybot.domain.ports import StorageError
from blybot.domain.subscriptions import Subscription
from tests._sql_fakes import FakeSubscriptionsDb

NOW = datetime(2026, 7, 25, 8, 0, tzinfo=UTC)


def make() -> tuple[ToolsDbSubscriptions, FakeSubscriptionsDb]:
    db = FakeSubscriptionsDb()
    return ToolsDbSubscriptions(runner=db), db


def sub(
    sub_id: str = "s1",
    dm: int = 500,
    chat: int = -100,
    last_run: datetime | None = None,
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


async def test_bootstrap_creates_the_table() -> None:
    store, db = make()
    await store.bootstrap()
    assert db.schema_created
    assert not db.schema_migrated  # a fresh table is already in final shape


async def test_bootstrap_backfills_a_legacy_int_only_row_and_is_idempotent() -> None:
    """A pre-existing subscription with ONLY the ints set is readable via the string keys."""
    store, db = make()
    db.schema_created = True  # an old int-keyed table...
    db.dm_channel_in_by_user = False
    db.chat_id_nullable = False
    db.seed_legacy("s1", dm_chat_id=500, chat_id=-100)

    await store.bootstrap()
    await store.bootstrap()  # second pass is a no-op (backfill guards hold)

    mine = await store.list_for_user(Scope("telegram", "500"))
    assert [s.sub_id for s in mine] == ["s1"]  # the DM string key resolves the pre-existing row
    assert int(mine[0].scope.channel) == -100  # group channel backfilled from chat_id
    assert db.schema_migrated


async def test_add_dual_writes_both_identities() -> None:
    """A freshly added subscription carries both string identities AND the legacy ints."""
    store, db = make()
    await store.add(sub("s1", dm=500, chat=-100))
    row = db.rows["s1"]
    assert (row["dm_platform"], row["dm_channel"]) == ("telegram", "500")
    assert (row["platform"], row["channel"], row["thread"]) == ("telegram", "-100", "")
    assert (row["dm_chat_id"], row["chat_id"], row["thread_id"]) == (500, -100, 0)


async def test_add_list_and_round_trip() -> None:
    store, _ = make()
    await store.add(sub("s1", dm=500))
    await store.add(sub("s2", dm=500, last_run=NOW))
    await store.add(sub("s3", dm=999))

    mine = await store.list_for_user(Scope("telegram", "500"))
    assert [s.sub_id for s in mine] == ["s1", "s2"]
    assert mine[1].last_run == NOW  # last_run round-trips through ISO text
    assert mine[0].schedule.token == "daily@08:00"  # noqa: S105 -- schedule token, not a secret
    assert {s.sub_id for s in await store.list_all()} == {"s1", "s2", "s3"}


async def test_remove_is_scoped_to_the_owner() -> None:
    store, _ = make()
    await store.add(sub("s1", dm=500))
    assert await store.remove(Scope("telegram", "999"), "s1") is False  # not owner: nothing removed
    assert [s.sub_id for s in await store.list_all()] == ["s1"]
    assert await store.remove(Scope("telegram", "500"), "s1") is True
    assert await store.list_all() == []


async def test_stamp_advances_the_watermark() -> None:
    store, _ = make()
    await store.add(sub("s1", dm=500))
    await store.stamp("s1", NOW)
    (stored,) = await store.list_all()
    assert stored.last_run == NOW


async def test_migrate_rekeys_a_groups_subscriptions() -> None:
    store, _ = make()
    await store.add(sub("s1", chat=-100))
    await store.add(sub("s2", chat=-200))
    await store.migrate(Scope("telegram", "-100"), Scope("telegram", "-100999"))
    assert {s.sub_id: int(s.scope.channel) for s in await store.list_all()} == {
        "s1": -100999,
        "s2": -200,
    }


async def test_storage_outage_raises_storage_error() -> None:
    store, db = make()
    db.fail = True
    with pytest.raises(StorageError):
        await store.add(sub())
    with pytest.raises(StorageError):
        await store.list_all()
    with pytest.raises(StorageError):
        await store.migrate(
            Scope("telegram", "-1"), Scope("telegram", "-2")
        )  # covers the _run_tx error path
