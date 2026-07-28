"""ToolsDbArchive tests against a SQL-level fake (same seam as the store)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from blybot.adapters.toolsdb.archive import (
    MESSAGES_ADD_CHANNEL,
    MESSAGES_ADD_PLATFORM,
    MESSAGES_ADD_THREAD,
    MESSAGES_BACKFILL_IDENTITY,
    MESSAGES_CHAT_ID_NULLABLE,
    MESSAGES_REBUILD_BY_TIME,
    MESSAGES_REBUILD_PK,
    MESSAGES_SCHEMA,
    MESSAGES_THREAD_ID_NULLABLE,
    Q_CHANNEL_IN_BY_TIME,
    Q_CHANNEL_IN_PK,
    Q_CHAT_ID_NULLABLE,
    Q_COUNT,
    Q_COUNT_BEFORE,
    Q_MIGRATE,
    Q_MIGRATE_CLEAR,
    Q_PURGE,
    Q_PURGE_BEFORE,
    Q_STORE,
    Q_TOTAL,
    Q_WINDOW,
    ToolsDbArchive,
)
from blybot.domain.models import CapturedMessage, Scope
from blybot.domain.ports import StorageError

NOW = datetime(2026, 7, 25, 12, 0, tzinfo=UTC)


class FakeMessagesDb:
    """Interprets the archive's exact query constants against a list of rows.

    Rows are dicts carrying both the string identity and the legacy ints,
    so the fake can model dual-write and the in-place migration.
    """

    def __init__(self) -> None:
        self.rows: list[dict[str, Any]] = []
        self.channel_in_pk = False  # old table: PK is still (chat_id, thread_id, message_id)
        self.channel_in_by_time = False  # old table: by_time is still (chat_id, thread_id, ...)
        self.chat_id_nullable = False  # old table: the ints are NOT NULL
        self.schema_created = False
        self.schema_migrated = False
        self.fail = False

    def seed_legacy(
        self, chat_id: int, thread_id: int, message_id: int, posted_at: Any, **extra: Any
    ) -> dict[str, Any]:
        """Append a pre-migration row: only the ints set, string key blank."""
        row = {
            "platform": "telegram",
            "channel": "",
            "thread": "",
            "chat_id": chat_id,
            "thread_id": thread_id,
            "message_id": message_id,
            # the DATETIME column holds naive UTC, as the adapter writes it
            "posted_at": posted_at.astimezone(UTC).replace(tzinfo=None),
            "author": extra.get("author", ""),
            "kind": extra.get("kind", "text"),
            "text": extra.get("text"),
            "reply_to": extra.get("reply_to"),
        }
        self.rows.append(row)
        return row

    def _scope_rows(
        self, platform: str, channel: str, thread: str, before: Any = None
    ) -> list[dict[str, Any]]:
        return [
            row
            for row in self.rows
            if (row["platform"], row["channel"], row["thread"]) == (platform, channel, thread)
            and (before is None or row["posted_at"] < before)
        ]

    def run_tx(self, statements: list[tuple[str, tuple[Any, ...]]]) -> None:
        for query, params in statements:
            self.run(query, params)

    def run(self, query: str, params: tuple[Any, ...]) -> list[tuple[Any, ...]]:
        if self.fail:
            msg = "db down"
            raise OSError(msg)
        assert query.count("%s") == len(params), f"placeholder/param mismatch: {query!r}"
        if query == MESSAGES_SCHEMA:
            if not self.schema_created:  # CREATE IF NOT EXISTS: no-op on old tables
                self.schema_created = True
                self.channel_in_pk = True  # a fresh table already has the string identity...
                self.channel_in_by_time = True
                self.chat_id_nullable = True  # ...and nullable ints
            return []
        if query in (MESSAGES_ADD_PLATFORM, MESSAGES_ADD_CHANNEL, MESSAGES_ADD_THREAD):
            return []  # column add: no-op (columns always present in the fake row)
        if query == MESSAGES_BACKFILL_IDENTITY:
            for row in self.rows:
                if row["channel"] == "" and row["chat_id"] is not None:
                    row["channel"] = str(row["chat_id"])
                    row["thread"] = "" if row["thread_id"] == 0 else str(row["thread_id"])
            return []
        if query == Q_CHANNEL_IN_PK:
            return [(1 if self.channel_in_pk else 0,)]
        if query == MESSAGES_REBUILD_PK:
            self.channel_in_pk = True
            self.schema_migrated = True
            return []
        if query == Q_CHANNEL_IN_BY_TIME:
            return [(1 if self.channel_in_by_time else 0,)]
        if query == MESSAGES_REBUILD_BY_TIME:
            self.channel_in_by_time = True
            self.schema_migrated = True
            return []
        if query == Q_CHAT_ID_NULLABLE:
            return [("YES" if self.chat_id_nullable else "NO",)]
        if query in (MESSAGES_CHAT_ID_NULLABLE, MESSAGES_THREAD_ID_NULLABLE):
            self.chat_id_nullable = True
            self.schema_migrated = True
            return []
        if query == Q_STORE:
            return self._store(params)
        if query == Q_WINDOW:
            platform, channel, thread, since, until = params
            hits = [
                (
                    row["message_id"],
                    row["posted_at"],
                    row["author"],
                    row["kind"],
                    row["text"],
                    row["reply_to"],
                )
                for row in self._scope_rows(platform, channel, thread)
                if since <= row["posted_at"] < until
            ]
            return sorted(hits, key=lambda row: (row[1], row[0]))
        if query == Q_COUNT:
            return [(len(self._scope_rows(*params)),)]
        if query == Q_COUNT_BEFORE:
            return [(len(self._scope_rows(params[0], params[1], params[2], before=params[3])),)]
        if query == Q_PURGE:
            for row in self._scope_rows(*params):
                self.rows.remove(row)
            return []
        if query == Q_PURGE_BEFORE:
            for row in self._scope_rows(params[0], params[1], params[2], before=params[3]):
                self.rows.remove(row)
            return []
        if query == Q_TOTAL:
            return [(len(self.rows),)]
        if query == Q_MIGRATE_CLEAR:
            platform, channel = params
            self.rows = [
                r for r in self.rows if (r["platform"], r["channel"]) != (platform, channel)
            ]
            return []
        if query == Q_MIGRATE:
            new_channel, new_chat_id, platform, old_channel = params
            for row in self.rows:
                if (row["platform"], row["channel"]) == (platform, old_channel):
                    row["channel"], row["chat_id"] = new_channel, new_chat_id
            return []
        msg = f"unexpected query: {query!r}"
        raise AssertionError(msg)

    def _store(self, params: tuple[Any, ...]) -> list[tuple[Any, ...]]:
        (
            platform,
            channel,
            thread,
            chat_id,
            thread_id,
            message_id,
            posted_at,
            author,
            kind,
            text,
            reply_to,
        ) = params
        key = (platform, channel, thread, message_id)
        if any(
            (r["platform"], r["channel"], r["thread"], r["message_id"]) == key for r in self.rows
        ):
            return []  # INSERT IGNORE: first stored version wins
        self.rows.append(
            {
                "platform": platform,
                "channel": channel,
                "thread": thread,
                "chat_id": chat_id,
                "thread_id": thread_id,
                "message_id": message_id,
                "posted_at": posted_at,
                "author": author,
                "kind": kind,
                "text": text,
                "reply_to": reply_to,
            }
        )
        return []


def make_archive() -> tuple[ToolsDbArchive, FakeMessagesDb]:
    db = FakeMessagesDb()
    return ToolsDbArchive(runner=db), db


def msg(message_id: int, minutes: int = 0, **extra: Any) -> CapturedMessage:
    return CapturedMessage(
        scope=Scope("telegram", "-1"),
        message_id=message_id,
        posted_at=NOW + timedelta(minutes=minutes),
        author=extra.pop("author", "abc123"),
        **extra,
    )


async def test_bootstrap_creates_the_messages_table() -> None:
    archive, db = make_archive()
    await archive.bootstrap()
    assert db.schema_created
    assert not db.schema_migrated  # a fresh table is already in final shape


async def test_bootstrap_backfills_a_legacy_int_only_row_and_is_idempotent() -> None:
    """A pre-existing message with ONLY the ints set is readable via the string key."""
    archive, db = make_archive()
    db.schema_created = True  # an old int-keyed table...
    db.channel_in_pk = False
    db.channel_in_by_time = False
    db.chat_id_nullable = False
    db.seed_legacy(-1, 0, 1, NOW, text="legacy", author="abc")
    db.seed_legacy(-1, 7, 2, NOW, author="topic")

    await archive.bootstrap()
    await archive.bootstrap()  # second pass is a no-op (backfill guard holds)

    span = (NOW - timedelta(hours=1), NOW + timedelta(hours=1))
    window = await archive.window(Scope("telegram", "-1"), *span)
    assert [m.message_id for m in window] == [1]  # channel default resolves the pre-existing row
    topic = await archive.window(Scope("telegram", "-1", "7"), *span)
    assert [m.message_id for m in topic] == [2]  # thread_id 7 backfilled to thread "7"
    assert db.schema_migrated


async def test_store_dual_writes_both_identities() -> None:
    """A freshly stored message carries the string identity AND the legacy ints."""
    archive, db = make_archive()
    await archive.store(
        CapturedMessage(scope=Scope("telegram", "-1", "7"), message_id=1, posted_at=NOW, author="x")
    )
    (row,) = db.rows
    assert (row["platform"], row["channel"], row["thread"]) == ("telegram", "-1", "7")
    assert (row["chat_id"], row["thread_id"]) == (-1, 7)  # ints dual-written for rollback


async def test_messages_round_trip_with_utc_restored() -> None:
    archive, _db = make_archive()
    stored = msg(1, text="hello", reply_to=7)

    await archive.store(stored)
    (loaded,) = await archive.window(
        Scope("telegram", "-1"), NOW - timedelta(hours=1), NOW + timedelta(hours=1)
    )

    assert loaded == stored
    assert loaded.posted_at.tzinfo is UTC


async def test_window_filters_by_time_and_scope_and_orders_oldest_first() -> None:
    archive, _db = make_archive()
    await archive.store(msg(3, minutes=30))
    await archive.store(msg(1, minutes=0))
    await archive.store(msg(2, minutes=200))  # outside the window
    await archive.store(
        CapturedMessage(scope=Scope("telegram", "-2"), message_id=9, posted_at=NOW, author="x")
    )

    window = await archive.window(Scope("telegram", "-1"), NOW, NOW + timedelta(hours=1))

    assert [m.message_id for m in window] == [1, 3]


async def test_redelivered_updates_are_idempotent() -> None:
    archive, _db = make_archive()
    await archive.store(msg(1, text="first"))
    await archive.store(msg(1, text="edited"))  # same id: first stored version wins

    (loaded,) = await archive.window(
        Scope("telegram", "-1"), NOW - timedelta(hours=1), NOW + timedelta(hours=1)
    )
    assert loaded.text == "first"


async def test_purge_deletes_only_the_scope_and_reports_the_count() -> None:
    archive, db = make_archive()
    await archive.store(msg(1))
    await archive.store(msg(2))
    await archive.store(
        CapturedMessage(scope=Scope("telegram", "-2"), message_id=9, posted_at=NOW, author="x")
    )

    assert await archive.purge(Scope("telegram", "-1")) == 2
    assert len(db.rows) == 1  # the other scope's archive is untouched


async def test_media_notes_store_without_text() -> None:
    archive, _db = make_archive()
    await archive.store(msg(1, kind="media_note", text=""))

    (loaded,) = await archive.window(
        Scope("telegram", "-1"), NOW - timedelta(hours=1), NOW + timedelta(hours=1)
    )
    assert loaded.kind == "media_note"
    assert loaded.text == ""
    assert loaded.reply_to is None


async def test_database_failure_raises_storage_error() -> None:
    archive, db = make_archive()
    db.fail = True
    with pytest.raises(StorageError):
        await archive.store(msg(1))
    with pytest.raises(StorageError):
        await archive.window(Scope("telegram", "-1"), NOW, NOW)
    with pytest.raises(StorageError):
        await archive.purge(Scope("telegram", "-1"))
    with pytest.raises(StorageError):
        await archive.migrate(
            Scope("telegram", "-1"), Scope("telegram", "-2")
        )  # the transactional path degrades the same way


async def test_purge_before_trims_only_the_older_rows() -> None:
    archive, _db = make_archive()
    await archive.store(msg(1, minutes=0))
    await archive.store(msg(2, minutes=90))

    removed = await archive.purge(Scope("telegram", "-1"), before=NOW + timedelta(hours=1))

    assert removed == 1
    window = await archive.window(
        Scope("telegram", "-1"), NOW - timedelta(days=1), NOW + timedelta(days=1)
    )
    assert [m.message_id for m in window] == [2]


async def test_total_counts_every_scope() -> None:
    archive, _db = make_archive()
    assert await archive.total() == 0
    await archive.store(msg(1))
    await archive.store(
        CapturedMessage(scope=Scope("telegram", "-2"), message_id=9, posted_at=NOW, author="x")
    )
    assert await archive.total() == 2


async def test_migrate_rekeys_every_topic_and_clears_collisions() -> None:
    archive, db = make_archive()
    await archive.store(msg(1))
    await archive.store(
        CapturedMessage(scope=Scope("telegram", "-1", "7"), message_id=2, posted_at=NOW, author="x")
    )
    # A message captured under the new id before the service message
    # arrived: the migrated rows win, mirroring the profile store.
    await archive.store(
        CapturedMessage(scope=Scope("telegram", "-100999"), message_id=3, posted_at=NOW, author="x")
    )

    await archive.migrate(Scope("telegram", "-1"), Scope("telegram", "-100999"))

    assert sorted((r["channel"], r["thread"], r["message_id"]) for r in db.rows) == [
        ("-100999", "", 1),
        ("-100999", "7", 2),
    ]
