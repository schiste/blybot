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

from blybot.adapters.toolsdb.store import _target
from blybot.domain.models import CapturedMessage
from blybot.domain.ports import StorageError
from blybot.observability import log_event

if TYPE_CHECKING:
    from blybot.adapters.toolsdb.store import SqlRunner
    from blybot.domain.models import Scope

MESSAGES_SCHEMA: Final = """
CREATE TABLE IF NOT EXISTS messages (
    chat_id    BIGINT      NOT NULL,
    thread_id  BIGINT      NOT NULL DEFAULT 0,
    message_id BIGINT      NOT NULL,
    posted_at  DATETIME    NOT NULL,
    author     VARCHAR(32) NOT NULL DEFAULT '',
    kind       VARCHAR(16) NOT NULL,
    text       TEXT        NULL,
    reply_to   BIGINT      NULL,
    PRIMARY KEY (chat_id, thread_id, message_id),
    KEY by_time (chat_id, thread_id, posted_at)
)
"""

_KEY: Final = "chat_id = %s AND thread_id = %s"
# INSERT IGNORE: a redelivered update must not fail the poll loop; the
# first stored version of a message wins. Neither edits nor deletions are
# reflected in the archive — edits by v3 design, and deletions because the
# Telegram Bot API never reports them to bots at all. `/capture purge` is
# therefore the only erasure path.
Q_STORE: Final = """
INSERT IGNORE INTO messages
    (chat_id, thread_id, message_id, posted_at, author, kind, text, reply_to)
VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
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
# recorded under the new id (a race can capture there first), then
# re-key. The old id never resolves again, so orphans must not exist —
# they would be invisible to analyses and unreachable by /capture purge.
Q_MIGRATE_CLEAR: Final = "DELETE FROM messages WHERE chat_id = %s"
Q_MIGRATE: Final = "UPDATE messages SET chat_id = %s WHERE chat_id = %s"


class ToolsDbArchive:
    """MessageArchive backed by the ``messages`` table."""

    def __init__(self, runner: SqlRunner) -> None:
        self._runner = runner

    async def bootstrap(self) -> None:
        """Create the messages table; idempotent, safe on every startup."""
        await self._run(MESSAGES_SCHEMA, ())

    async def store(self, message: CapturedMessage) -> None:
        """Persist one captured message (idempotent per message id)."""
        chat_id, thread_id = _target(message.scope)
        await self._run(
            Q_STORE,
            (
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
        chat_id, thread_id = _target(scope)
        rows = await self._run(
            Q_WINDOW,
            (
                chat_id,
                thread_id,
                since.astimezone(UTC).replace(tzinfo=None),
                until.astimezone(UTC).replace(tzinfo=None),
            ),
        )
        return [_message_from_row(scope, row) for row in rows]

    async def purge(self, scope: Scope, before: datetime | None = None) -> int:
        """Hard-delete the scope's archive (older than ``before`` if given)."""
        chat_id, thread_id = _target(scope)
        if before is None:
            count_query, purge_query = Q_COUNT, Q_PURGE
            params: tuple[Any, ...] = (chat_id, thread_id)
        else:
            count_query, purge_query = Q_COUNT_BEFORE, Q_PURGE_BEFORE
            params = (chat_id, thread_id, before.astimezone(UTC).replace(tzinfo=None))
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
        old_chat_id, new_chat_id = int(old.channel), int(new.channel)
        await self._run_tx(
            [(Q_MIGRATE_CLEAR, (new_chat_id,)), (Q_MIGRATE, (new_chat_id, old_chat_id))]
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
