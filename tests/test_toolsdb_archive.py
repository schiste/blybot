"""ToolsDbArchive tests against a SQL-level fake (same seam as the store)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from blybot.adapters.toolsdb.archive import ToolsDbArchive
from blybot.domain.models import CapturedMessage, Scope
from blybot.domain.ports import StorageError
from tests._sql_fakes import FakeMessagesDb

NOW = datetime(2026, 7, 25, 12, 0, tzinfo=UTC)


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
