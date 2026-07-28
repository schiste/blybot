"""ToolsDbSubscriptions adapter tests (the durable DM-chat-id carve-out store)."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import pytest

from blybot.adapters.toolsdb.subscriptions import (
    Q_ADD,
    Q_CHAT_ID_NULLABLE,
    Q_DELETE,
    Q_DM_CHANNEL_IN_BY_USER,
    Q_LIST_ALL,
    Q_LIST_FOR_USER,
    Q_MIGRATE,
    Q_OWNED,
    Q_STAMP,
    SUBS_ADD_CHANNEL,
    SUBS_ADD_DM_CHANNEL,
    SUBS_ADD_DM_PLATFORM,
    SUBS_ADD_PLATFORM,
    SUBS_ADD_THREAD,
    SUBS_BACKFILL_DM,
    SUBS_BACKFILL_SCOPE,
    SUBS_CHAT_ID_NULLABLE,
    SUBS_DM_CHAT_ID_NULLABLE,
    SUBS_REBUILD_BY_USER,
    SUBS_THREAD_ID_NULLABLE,
    SUBSCRIPTIONS_SCHEMA,
    ToolsDbSubscriptions,
)
from blybot.domain.models import Schedule, Scope
from blybot.domain.ports import StorageError
from blybot.domain.subscriptions import Subscription

NOW = datetime(2026, 7, 25, 8, 0, tzinfo=UTC)


@dataclass
class FakeSubscriptionsDb:
    """Interprets the adapter's exact query constants against a dict of rows.

    Each row is a dict carrying both string identities (DM and group) and
    the legacy ints, so the fake can model dual-write and the migration.
    """

    rows: dict[str, dict[str, Any]] = field(default_factory=dict)  # sub_id -> row
    dm_channel_in_by_user: bool = False  # old table: by_user is still (dm_chat_id)
    chat_id_nullable: bool = False  # old table: the ints are NOT NULL
    schema_created: bool = False
    schema_migrated: bool = False
    fail: bool = False

    def seed_legacy(
        self, sub_id: str, dm_chat_id: int, chat_id: int, thread_id: int = 0, **extra: Any
    ) -> None:
        """Insert a pre-migration row: only the ints set, string keys blank."""
        self.rows[sub_id] = {
            "sub_id": sub_id,
            "dm_platform": "telegram",
            "dm_channel": "",
            "platform": "telegram",
            "channel": "",
            "thread": "",
            "dm_chat_id": dm_chat_id,
            "chat_id": chat_id,
            "thread_id": thread_id,
            "schedule": extra.get("schedule", "daily@08:00"),
            "recipe": extra.get("recipe", "summarize"),
            "lang": extra.get("lang", "en"),
            "last_run": extra.get("last_run"),
        }

    def _select_row(self, row: dict[str, Any]) -> tuple[Any, ...]:
        return (
            row["sub_id"],
            row["dm_platform"],
            row["dm_channel"],
            row["platform"],
            row["channel"],
            row["thread"],
            row["schedule"],
            row["recipe"],
            row["lang"],
            row["last_run"],
        )

    def run_tx(self, statements: list[tuple[str, tuple[Any, ...]]]) -> None:
        for query, params in statements:
            self.run(query, params)

    def run(self, query: str, params: tuple[Any, ...]) -> list[tuple[Any, ...]]:
        if self.fail:
            msg = "db down"
            raise OSError(msg)
        assert query.count("%s") == len(params), f"placeholder/param mismatch: {query!r}"
        migrated = self._run_migration(query)
        if migrated is not None:
            return migrated
        if query == Q_ADD:
            (
                sub_id,
                dm_platform,
                dm_channel,
                platform,
                channel,
                thread,
                dm_chat_id,
                chat_id,
                thread_id,
                schedule,
                recipe,
                lang,
                last_run,
            ) = params
            self.rows[sub_id] = {
                "sub_id": sub_id,
                "dm_platform": dm_platform,
                "dm_channel": dm_channel,
                "platform": platform,
                "channel": channel,
                "thread": thread,
                "dm_chat_id": dm_chat_id,
                "chat_id": chat_id,
                "thread_id": thread_id,
                "schedule": schedule,
                "recipe": recipe,
                "lang": lang,
                "last_run": last_run,
            }
            return []
        if query == Q_OWNED:
            sub_id, dm_platform, dm_channel = params
            row = self.rows.get(sub_id)
            owns = row is not None and (row["dm_platform"], row["dm_channel"]) == (
                dm_platform,
                dm_channel,
            )
            return [(sub_id,)] if owns else []
        if query == Q_DELETE:
            sub_id, dm_platform, dm_channel = params
            row = self.rows.get(sub_id)
            if row is not None and (row["dm_platform"], row["dm_channel"]) == (
                dm_platform,
                dm_channel,
            ):
                del self.rows[sub_id]
            return []
        if query == Q_LIST_FOR_USER:
            dm_platform, dm_channel = params
            mine = [
                r
                for r in self.rows.values()
                if (r["dm_platform"], r["dm_channel"]) == (dm_platform, dm_channel)
            ]
            return [self._select_row(r) for r in sorted(mine, key=lambda r: r["sub_id"])]
        if query == Q_LIST_ALL:
            ordered = sorted(self.rows.values(), key=lambda r: r["sub_id"])
            return [self._select_row(r) for r in ordered]
        if query == Q_STAMP:
            last_run, sub_id = params
            if sub_id in self.rows:
                self.rows[sub_id]["last_run"] = last_run
            return []
        if query == Q_MIGRATE:
            new_channel, new_chat_id, platform, old_channel = params
            for row in self.rows.values():
                if (row["platform"], row["channel"]) == (platform, old_channel):
                    row["channel"], row["chat_id"] = new_channel, new_chat_id
            return []
        pytest.fail(f"unexpected query: {query}")

    def _run_migration(self, query: str) -> list[tuple[Any, ...]] | None:
        """Handle schema/migration statements; return None for data queries."""
        if query == SUBSCRIPTIONS_SCHEMA:
            if not self.schema_created:  # CREATE IF NOT EXISTS: no-op on old tables
                self.schema_created = True
                self.dm_channel_in_by_user = True  # a fresh table already has the string index...
                self.chat_id_nullable = True  # ...and nullable ints
            return []
        if query in (
            SUBS_ADD_DM_PLATFORM,
            SUBS_ADD_DM_CHANNEL,
            SUBS_ADD_PLATFORM,
            SUBS_ADD_CHANNEL,
            SUBS_ADD_THREAD,
        ):
            return []  # column add: no-op (columns always present in the fake row)
        if query == SUBS_BACKFILL_SCOPE:
            for row in self.rows.values():
                if row["channel"] == "" and row["chat_id"] is not None:
                    row["channel"] = str(row["chat_id"])
                    row["thread"] = "" if row["thread_id"] == 0 else str(row["thread_id"])
            return []
        if query == SUBS_BACKFILL_DM:
            for row in self.rows.values():
                if row["dm_channel"] == "" and row["dm_chat_id"] is not None:
                    row["dm_channel"] = str(row["dm_chat_id"])
            return []
        if query == Q_DM_CHANNEL_IN_BY_USER:
            return [(1 if self.dm_channel_in_by_user else 0,)]
        if query == SUBS_REBUILD_BY_USER:
            self.dm_channel_in_by_user = True
            self.schema_migrated = True
            return []
        if query == Q_CHAT_ID_NULLABLE:
            return [("YES" if self.chat_id_nullable else "NO",)]
        if query in (SUBS_CHAT_ID_NULLABLE, SUBS_THREAD_ID_NULLABLE, SUBS_DM_CHAT_ID_NULLABLE):
            self.chat_id_nullable = True
            self.schema_migrated = True
            return []
        return None


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
