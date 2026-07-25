"""ToolsDbArchive tests against a SQL-level fake (same seam as the store)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from blybot.adapters.toolsdb.archive import (
    MESSAGES_SCHEMA,
    Q_COUNT,
    Q_PURGE,
    Q_STORE,
    Q_WINDOW,
    ToolsDbArchive,
)
from blybot.domain.models import CapturedMessage
from blybot.domain.ports import StorageError

NOW = datetime(2026, 7, 25, 12, 0, tzinfo=UTC)


class FakeMessagesDb:
    """Interprets the archive's exact query constants against a dict."""

    def __init__(self) -> None:
        self.rows: dict[tuple[int, int, int], tuple[Any, ...]] = {}
        self.schema_created = False
        self.fail = False

    def run(self, query: str, params: tuple[Any, ...]) -> list[tuple[Any, ...]]:
        if self.fail:
            msg = "db down"
            raise OSError(msg)
        assert query.count("%s") == len(params), f"placeholder/param mismatch: {query!r}"
        if query == MESSAGES_SCHEMA:
            self.schema_created = True
            return []
        if query == Q_STORE:
            chat_id, thread_id, message_id = params[0], params[1], params[2]
            self.rows.setdefault((chat_id, thread_id, message_id), params)  # INSERT IGNORE
            return []
        if query == Q_WINDOW:
            chat_id, thread_id, since, until = params
            hits = [
                (row[2], row[3], row[4], row[5], row[6], row[7])
                for row in self.rows.values()
                if row[0] == chat_id and row[1] == thread_id and since <= row[3] < until
            ]
            return sorted(hits, key=lambda row: (row[1], row[0]))
        if query == Q_COUNT:
            return [(len(self._scope_rows(params)),)]
        if query == Q_PURGE:
            for key in self._scope_rows(params):
                del self.rows[key]
            return []
        msg = f"unexpected query: {query!r}"
        raise AssertionError(msg)

    def _scope_rows(self, params: tuple[Any, ...]) -> list[tuple[int, int, int]]:
        chat_id, thread_id = params
        return [key for key in self.rows if key[0] == chat_id and key[1] == thread_id]


def make_archive() -> tuple[ToolsDbArchive, FakeMessagesDb]:
    db = FakeMessagesDb()
    return ToolsDbArchive(runner=db), db


def msg(message_id: int, minutes: int = 0, **extra: Any) -> CapturedMessage:
    return CapturedMessage(
        chat_id=-1,
        thread_id=0,
        message_id=message_id,
        posted_at=NOW + timedelta(minutes=minutes),
        author=extra.pop("author", "abc123"),
        **extra,
    )


async def test_bootstrap_creates_the_messages_table() -> None:
    archive, db = make_archive()
    await archive.bootstrap()
    assert db.schema_created


async def test_messages_round_trip_with_utc_restored() -> None:
    archive, _db = make_archive()
    stored = msg(1, text="hello", reply_to=7)

    await archive.store(stored)
    (loaded,) = await archive.window(-1, 0, NOW - timedelta(hours=1), NOW + timedelta(hours=1))

    assert loaded == stored
    assert loaded.posted_at.tzinfo is UTC


async def test_window_filters_by_time_and_scope_and_orders_oldest_first() -> None:
    archive, _db = make_archive()
    await archive.store(msg(3, minutes=30))
    await archive.store(msg(1, minutes=0))
    await archive.store(msg(2, minutes=200))  # outside the window
    await archive.store(
        CapturedMessage(chat_id=-2, thread_id=0, message_id=9, posted_at=NOW, author="x")
    )

    window = await archive.window(-1, 0, NOW, NOW + timedelta(hours=1))

    assert [m.message_id for m in window] == [1, 3]


async def test_redelivered_updates_are_idempotent() -> None:
    archive, _db = make_archive()
    await archive.store(msg(1, text="first"))
    await archive.store(msg(1, text="edited"))  # same id: first stored version wins

    (loaded,) = await archive.window(-1, 0, NOW - timedelta(hours=1), NOW + timedelta(hours=1))
    assert loaded.text == "first"


async def test_purge_deletes_only_the_scope_and_reports_the_count() -> None:
    archive, db = make_archive()
    await archive.store(msg(1))
    await archive.store(msg(2))
    await archive.store(
        CapturedMessage(chat_id=-2, thread_id=0, message_id=9, posted_at=NOW, author="x")
    )

    assert await archive.purge(-1, 0) == 2
    assert len(db.rows) == 1  # the other scope's archive is untouched


async def test_media_notes_store_without_text() -> None:
    archive, _db = make_archive()
    await archive.store(msg(1, kind="media_note", text=""))

    (loaded,) = await archive.window(-1, 0, NOW - timedelta(hours=1), NOW + timedelta(hours=1))
    assert loaded.kind == "media_note"
    assert loaded.text == ""
    assert loaded.reply_to is None


async def test_database_failure_raises_storage_error() -> None:
    archive, db = make_archive()
    db.fail = True
    with pytest.raises(StorageError):
        await archive.store(msg(1))
    with pytest.raises(StorageError):
        await archive.window(-1, 0, NOW, NOW)
    with pytest.raises(StorageError):
        await archive.purge(-1, 0)
