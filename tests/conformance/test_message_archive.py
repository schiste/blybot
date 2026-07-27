"""MessageArchive contract — every implementation proves these semantics.

Runs against BOTH the in-memory fake and the real ToolsDbArchive (over its
SQL-level fake). Idempotency, windowing, purge counts, and re-keying are the
port's meaning; an impl that drifts fails here.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from blybot.domain.models import CapturedMessage, Scope
from blybot.domain.ports import MessageArchive
from tests.conformance._impls import MESSAGE_ARCHIVES

NOW = datetime(2026, 7, 25, 12, 0, tzinfo=UTC)
SCOPE = Scope("telegram", "-1")


@pytest.fixture(
    params=[build for _, build in MESSAGE_ARCHIVES], ids=[name for name, _ in MESSAGE_ARCHIVES]
)
def archive(request: pytest.FixtureRequest) -> MessageArchive:
    build = request.param
    store: MessageArchive = build()
    return store


def _msg(
    message_id: int, minutes: int = 0, *, scope: Scope = SCOPE, text: str = ""
) -> CapturedMessage:
    return CapturedMessage(
        scope=scope,
        message_id=message_id,
        posted_at=NOW + timedelta(minutes=minutes),
        author="abc123",
        text=text,
    )


async def test_store_is_idempotent_per_message_id(archive: MessageArchive) -> None:
    await archive.store(_msg(1, text="first"))
    await archive.store(_msg(1, text="edited"))  # same id: INSERT IGNORE keeps the first

    (loaded,) = await archive.window(SCOPE, NOW - timedelta(hours=1), NOW + timedelta(hours=1))
    assert loaded.text == "first"


async def test_window_is_half_open_and_oldest_first(archive: MessageArchive) -> None:
    await archive.store(_msg(3, minutes=30))
    await archive.store(_msg(1, minutes=0))  # exactly `since`: included
    await archive.store(_msg(2, minutes=60))  # exactly `until`: excluded
    await archive.store(_msg(9, scope=Scope("telegram", "-2")))  # other scope

    window = await archive.window(SCOPE, NOW, NOW + timedelta(hours=1))

    assert [m.message_id for m in window] == [1, 3]  # since <= posted_at < until, oldest first


async def test_purge_deletes_the_scope_and_returns_the_count(archive: MessageArchive) -> None:
    await archive.store(_msg(1))
    await archive.store(_msg(2))
    await archive.store(_msg(9, scope=Scope("telegram", "-2")))

    assert await archive.purge(SCOPE) == 2  # returns rows removed
    assert await archive.window(SCOPE, NOW - timedelta(days=1), NOW + timedelta(days=1)) == []
    # The other scope's archive is untouched.
    other = await archive.window(
        Scope("telegram", "-2"), NOW - timedelta(days=1), NOW + timedelta(days=1)
    )
    assert [m.message_id for m in other] == [9]


async def test_purge_before_trims_only_the_older_rows(archive: MessageArchive) -> None:
    await archive.store(_msg(1, minutes=0))
    await archive.store(_msg(2, minutes=90))

    assert await archive.purge(SCOPE, before=NOW + timedelta(hours=1)) == 1
    window = await archive.window(SCOPE, NOW - timedelta(days=1), NOW + timedelta(days=1))
    assert [m.message_id for m in window] == [2]  # the newer row survives


async def test_migrate_rekeys_every_topic_by_channel(archive: MessageArchive) -> None:
    await archive.store(_msg(1))
    await archive.store(_msg(2, scope=Scope("telegram", "-1", "7")))

    await archive.migrate(Scope("telegram", "-1"), Scope("telegram", "-999"))

    assert await archive.window(SCOPE, NOW - timedelta(days=1), NOW + timedelta(days=1)) == []
    base = await archive.window(
        Scope("telegram", "-999"), NOW - timedelta(days=1), NOW + timedelta(days=1)
    )
    topic = await archive.window(
        Scope("telegram", "-999", "7"), NOW - timedelta(days=1), NOW + timedelta(days=1)
    )
    assert [m.message_id for m in base] == [1]
    assert [m.message_id for m in topic] == [2]
