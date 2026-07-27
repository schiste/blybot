"""ToolsDB-backed message archive (v3 plan §2.2).

One ``messages`` table holds the captured content of opted-in scopes.
Rows carry group structure, timestamps, text, and a pseudonym label —
never a Telegram user id, username, or display name; the pseudonym is
derived at the capture boundary before anything reaches this adapter.
Reuses the store's :class:`~blybot.adapters.toolsdb.store.SqlRunner`
seam, so tests drive it with the same SQL-level fake.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Final

import pymysql

from blybot.adapters.toolsdb.store import _keys, _target
from blybot.domain.models import CapturedMessage
from blybot.domain.ports import StorageError
from blybot.observability import log_event

if TYPE_CHECKING:
    from blybot.adapters.toolsdb.store import SqlRunner
    from blybot.domain.models import Scope

# Final shape for a fresh deployment: the opaque string identity is the
# key, the legacy telegram ints are nullable and dual-written for rollback
# safety. A brand-new table therefore skips every migration below.
MESSAGES_SCHEMA: Final = """
CREATE TABLE IF NOT EXISTS messages (
    platform   VARCHAR(32)  NOT NULL DEFAULT 'telegram',
    channel    VARCHAR(190) NOT NULL DEFAULT '',
    thread     VARCHAR(190) NOT NULL DEFAULT '',
    chat_id    BIGINT       NULL,
    thread_id  BIGINT       NULL,
    message_id BIGINT       NOT NULL,
    posted_at  DATETIME     NOT NULL,
    author     VARCHAR(32)  NOT NULL DEFAULT '',
    kind       VARCHAR(16)  NOT NULL,
    text       TEXT         NULL,
    reply_to   BIGINT       NULL,
    PRIMARY KEY (platform, channel, thread, message_id),
    KEY by_time (platform, channel, thread, posted_at)
)
"""

_KEY: Final = "platform = %s AND channel = %s AND thread = %s"
# INSERT IGNORE: a redelivered update must not fail the poll loop; the
# first stored version of a message wins. Neither edits nor deletions are
# reflected in the archive — edits by v3 design, and deletions because the
# Telegram Bot API never reports them to bots at all. `/capture purge` is
# therefore the only erasure path. Dual-writes the legacy ints alongside
# the string identity so a rollback still reads every row.
Q_STORE: Final = """
INSERT IGNORE INTO messages
    (platform, channel, thread, chat_id, thread_id, message_id, posted_at, author, kind, text,
     reply_to)
VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
"""
Q_WINDOW: Final = (
    "SELECT message_id, posted_at, author, kind, text, reply_to FROM messages "  # noqa: S608
    f"WHERE {_KEY} AND posted_at >= %s AND posted_at < %s "
    "ORDER BY posted_at, message_id"
)
Q_PURGE: Final = f"DELETE FROM messages WHERE {_KEY}"  # noqa: S608
Q_PURGE_BEFORE: Final = f"DELETE FROM messages WHERE {_KEY} AND posted_at < %s"  # noqa: S608
Q_COUNT: Final = f"SELECT COUNT(*) FROM messages WHERE {_KEY}"  # noqa: S608
Q_COUNT_BEFORE: Final = f"SELECT COUNT(*) FROM messages WHERE {_KEY} AND posted_at < %s"  # noqa: S608
Q_TOTAL: Final = "SELECT COUNT(*) FROM messages"
# Same shape as the profile store's migration: clear anything already
# recorded under the new channel (a race can capture there first), then
# re-key, moving the dual-written chat_id in lockstep. The old channel
# never resolves again, so orphans must not exist — they would be invisible
# to analyses and unreachable by /capture purge.
Q_MIGRATE_CLEAR: Final = "DELETE FROM messages WHERE platform = %s AND channel = %s"
Q_MIGRATE: Final = (
    "UPDATE messages SET channel = %s, chat_id = %s WHERE platform = %s AND channel = %s"
)

# Idempotent in-place schema upgrade for tables that predate the string
# identity (scope PR-2). Each ADD is a no-op once applied; the backfill
# touches only un-backfilled rows.
MESSAGES_ADD_PLATFORM: Final = (
    "ALTER TABLE messages ADD COLUMN IF NOT EXISTS platform VARCHAR(32) NOT NULL DEFAULT 'telegram'"
)
MESSAGES_ADD_CHANNEL: Final = (
    "ALTER TABLE messages ADD COLUMN IF NOT EXISTS channel VARCHAR(190) NOT NULL DEFAULT ''"
)
MESSAGES_ADD_THREAD: Final = (
    "ALTER TABLE messages ADD COLUMN IF NOT EXISTS thread VARCHAR(190) NOT NULL DEFAULT ''"
)
MESSAGES_BACKFILL_IDENTITY: Final = (
    "UPDATE messages SET channel = CAST(chat_id AS CHAR), "
    "thread = IF(thread_id = 0, '', CAST(thread_id AS CHAR)) "
    "WHERE channel = '' AND chat_id IS NOT NULL"
)
# Rebuild the primary key to the string identity, guarded so it runs
# exactly once (channel is not a PRIMARY column on an old table). Must
# precede the nullable conversion — a PRIMARY KEY column cannot be NULL.
Q_CHANNEL_IN_PK: Final = """
SELECT COUNT(*) FROM information_schema.STATISTICS
WHERE table_schema = DATABASE() AND table_name = 'messages'
  AND index_name = 'PRIMARY' AND column_name = 'channel'
"""
MESSAGES_REBUILD_PK: Final = (
    "ALTER TABLE messages DROP PRIMARY KEY, "
    "ADD PRIMARY KEY (platform, channel, thread, message_id)"
)
# Rebuild the by_time secondary index onto the string identity too, so the
# window query keeps an index on the largest table. Guarded the same way.
Q_CHANNEL_IN_BY_TIME: Final = """
SELECT COUNT(*) FROM information_schema.STATISTICS
WHERE table_schema = DATABASE() AND table_name = 'messages'
  AND index_name = 'by_time' AND column_name = 'channel'
"""
MESSAGES_REBUILD_BY_TIME: Final = (
    "ALTER TABLE messages DROP INDEX by_time, "
    "ADD INDEX by_time (platform, channel, thread, posted_at)"
)
# Relax the ints to nullable (a future non-telegram row leaves them NULL),
# once, guarded by an information_schema check.
Q_CHAT_ID_NULLABLE: Final = """
SELECT IS_NULLABLE FROM information_schema.COLUMNS
WHERE table_schema = DATABASE() AND table_name = 'messages'
  AND column_name = 'chat_id'
"""
MESSAGES_CHAT_ID_NULLABLE: Final = "ALTER TABLE messages MODIFY chat_id BIGINT NULL"
MESSAGES_THREAD_ID_NULLABLE: Final = "ALTER TABLE messages MODIFY thread_id BIGINT NULL"


class ToolsDbArchive:
    """MessageArchive backed by the ``messages`` table."""

    def __init__(self, runner: SqlRunner) -> None:
        self._runner = runner

    async def bootstrap(self) -> None:
        """Create the messages table, then migrate an older one in place.

        Idempotent: a fresh table is complete from ``MESSAGES_SCHEMA``; an
        older int-keyed table gains the string identity columns, has every
        row backfilled, its primary key and ``by_time`` index rebuilt onto
        the string identity, and its ints relaxed to nullable — dropping no
        rows. Every step no-ops once applied, so re-running is safe.
        """
        await self._run(MESSAGES_SCHEMA, ())
        await self._run(MESSAGES_ADD_PLATFORM, ())
        await self._run(MESSAGES_ADD_CHANNEL, ())
        await self._run(MESSAGES_ADD_THREAD, ())
        await self._run(MESSAGES_BACKFILL_IDENTITY, ())
        rows = await self._run(Q_CHANNEL_IN_PK, ())
        if rows and not int(rows[0][0]):
            await self._run(MESSAGES_REBUILD_PK, ())
            log_event("archive_migrated", "ok")
        rows = await self._run(Q_CHANNEL_IN_BY_TIME, ())
        if rows and not int(rows[0][0]):
            await self._run(MESSAGES_REBUILD_BY_TIME, ())
            log_event("archive_migrated", "ok")
        rows = await self._run(Q_CHAT_ID_NULLABLE, ())
        if rows and rows[0][0] == "NO":
            await self._run(MESSAGES_CHAT_ID_NULLABLE, ())
            await self._run(MESSAGES_THREAD_ID_NULLABLE, ())
            log_event("archive_migrated", "ok")

    async def store(self, message: CapturedMessage) -> None:
        """Persist one captured message (idempotent per message id)."""
        platform, channel, thread = _keys(message.scope)
        chat_id, thread_id = _target(message.scope)
        await self._run(
            Q_STORE,
            (
                platform,
                channel,
                thread,
                chat_id,
                thread_id,
                message.message_id,
                message.posted_at.astimezone(UTC).replace(tzinfo=None),
                message.author,
                message.kind,
                message.text or None,
                message.reply_to,
            ),
        )

    async def window(self, scope: Scope, since: datetime, until: datetime) -> list[CapturedMessage]:
        """Return the scope's messages with ``since <= posted_at < until``, oldest first."""
        rows = await self._run(
            Q_WINDOW,
            (
                *_keys(scope),
                since.astimezone(UTC).replace(tzinfo=None),
                until.astimezone(UTC).replace(tzinfo=None),
            ),
        )
        return [_message_from_row(scope, row) for row in rows]

    async def purge(self, scope: Scope, before: datetime | None = None) -> int:
        """Hard-delete the scope's archive (older than ``before`` if given)."""
        keys = _keys(scope)
        if before is None:
            count_query, purge_query = Q_COUNT, Q_PURGE
            params: tuple[Any, ...] = keys
        else:
            count_query, purge_query = Q_COUNT_BEFORE, Q_PURGE_BEFORE
            params = (*keys, before.astimezone(UTC).replace(tzinfo=None))
        rows = await self._run(count_query, params)
        count = int(rows[0][0]) if rows else 0
        await self._run(purge_query, params)
        return count

    async def total(self) -> int:
        """Return the archive's total row count (operator metric)."""
        rows = await self._run(Q_TOTAL, ())
        return int(rows[0][0]) if rows else 0

    async def migrate(self, old: Scope, new: Scope) -> None:
        """Re-key every topic's messages after a group→supergroup upgrade.

        Clear-then-rekey runs in one transaction so a crash between the two
        cannot drop the destination's rows while leaving the source's.
        """
        new_chat_id = int(new.channel)
        await self._run_tx(
            [
                (Q_MIGRATE_CLEAR, (new.platform, new.channel)),
                (Q_MIGRATE, (new.channel, new_chat_id, old.platform, old.channel)),
            ]
        )

    async def _run(self, query: str, params: tuple[Any, ...]) -> list[tuple[Any, ...]]:
        try:
            return await asyncio.to_thread(self._runner.run, query, params)
        except (pymysql.MySQLError, OSError, KeyError) as error:
            log_event("archive", "error")
            msg = "message archive unavailable"
            raise StorageError(msg) from error

    async def _run_tx(self, statements: list[tuple[str, tuple[Any, ...]]]) -> None:
        try:
            await asyncio.to_thread(self._runner.run_tx, statements)
        except (pymysql.MySQLError, OSError, KeyError) as error:
            log_event("archive", "error")
            msg = "message archive unavailable"
            raise StorageError(msg) from error


def _message_from_row(scope: Scope, row: tuple[Any, ...]) -> CapturedMessage:
    message_id, posted_at, author, kind, text, reply_to = row
    return CapturedMessage(
        scope=scope,
        message_id=int(message_id),
        posted_at=posted_at.replace(tzinfo=UTC),
        author=author or "",
        kind=kind,
        text=text or "",
        reply_to=int(reply_to) if reply_to is not None else None,
    )
