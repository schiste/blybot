"""ToolsDbSubscriptions adapter tests (the durable DM-chat-id carve-out store)."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import pytest

from blybot.adapters.toolsdb.subscriptions import (
    Q_ADD,
    Q_DELETE,
    Q_LIST_ALL,
    Q_LIST_FOR_USER,
    Q_MIGRATE,
    Q_OWNED,
    Q_STAMP,
    SUBSCRIPTIONS_SCHEMA,
    ToolsDbSubscriptions,
)
from blybot.domain.models import Schedule
from blybot.domain.ports import StorageError
from blybot.domain.subscriptions import Subscription

NOW = datetime(2026, 7, 25, 8, 0, tzinfo=UTC)


@dataclass
class FakeSubscriptionsDb:
    """Interprets the adapter's exact query constants against a dict."""

    rows: dict[str, tuple[Any, ...]] = field(default_factory=dict)  # sub_id -> row tuple
    schema_created: bool = False
    fail: bool = False

    def run_tx(self, statements: list[tuple[str, tuple[Any, ...]]]) -> None:
        for query, params in statements:
            self.run(query, params)

    def run(self, query: str, params: tuple[Any, ...]) -> list[tuple[Any, ...]]:
        if self.fail:
            msg = "db down"
            raise OSError(msg)
        assert query.count("%s") == len(params), f"placeholder/param mismatch: {query!r}"
        if query == SUBSCRIPTIONS_SCHEMA:
            self.schema_created = True
            return []
        if query == Q_ADD:
            self.rows[params[0]] = params
            return []
        if query == Q_OWNED:
            sub_id, dm = params
            row = self.rows.get(sub_id)
            return [(sub_id,)] if row is not None and row[1] == dm else []
        if query == Q_DELETE:
            sub_id, dm = params
            row = self.rows.get(sub_id)
            if row is not None and row[1] == dm:
                del self.rows[sub_id]
            return []
        if query == Q_LIST_FOR_USER:
            (dm,) = params
            return sorted((r for r in self.rows.values() if r[1] == dm), key=lambda r: r[0])
        if query == Q_LIST_ALL:
            return sorted(self.rows.values(), key=lambda r: r[0])
        if query == Q_STAMP:
            last_run, sub_id = params
            if sub_id in self.rows:
                self.rows[sub_id] = (*self.rows[sub_id][:7], last_run)
            return []
        if query == Q_MIGRATE:
            new_id, old_id = params
            for sub_id, row in list(self.rows.items()):
                if row[2] == old_id:
                    self.rows[sub_id] = (row[0], row[1], new_id, *row[3:])
            return []
        pytest.fail(f"unexpected query: {query}")


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
        dm_chat_id=dm,
        chat_id=chat,
        thread_id=0,
        schedule=Schedule(kind="daily", hour=8),
        recipe="summarize",
        lang="en",
        last_run=last_run,
    )


async def test_bootstrap_creates_the_table() -> None:
    store, db = make()
    await store.bootstrap()
    assert db.schema_created


async def test_add_list_and_round_trip() -> None:
    store, _ = make()
    await store.add(sub("s1", dm=500))
    await store.add(sub("s2", dm=500, last_run=NOW))
    await store.add(sub("s3", dm=999))

    mine = await store.list_for_user(500)
    assert [s.sub_id for s in mine] == ["s1", "s2"]
    assert mine[1].last_run == NOW  # last_run round-trips through ISO text
    assert mine[0].schedule.token == "daily@08:00"  # noqa: S105 -- schedule token, not a secret
    assert {s.sub_id for s in await store.list_all()} == {"s1", "s2", "s3"}


async def test_remove_is_scoped_to_the_owner() -> None:
    store, _ = make()
    await store.add(sub("s1", dm=500))
    assert await store.remove(999, "s1") is False  # not the owner: nothing removed
    assert [s.sub_id for s in await store.list_all()] == ["s1"]
    assert await store.remove(500, "s1") is True
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
    await store.migrate(-100, -100999)
    assert {s.sub_id: s.chat_id for s in await store.list_all()} == {"s1": -100999, "s2": -200}


async def test_storage_outage_raises_storage_error() -> None:
    store, db = make()
    db.fail = True
    with pytest.raises(StorageError):
        await store.add(sub())
    with pytest.raises(StorageError):
        await store.list_all()
    with pytest.raises(StorageError):
        await store.migrate(-1, -2)  # covers the _run_tx error path
