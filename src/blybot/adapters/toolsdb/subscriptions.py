"""ToolsDB-backed digest subscription store (SPECIFICATION §21).

The one durable store keyed by a subscriber's private (DM) chat id — the
sole persisted Telegram user identifier, an explicit opt-in R6 carve-out
(see :mod:`blybot.domain.subscriptions`). Kept in its own table, isolated
from the pseudonymized profile/archive stores. Reuses the store's
:class:`~blybot.adapters.toolsdb.store.SqlRunner` seam, so tests drive it
with the same SQL-level fake.
"""

from __future__ import annotations

import asyncio
from datetime import datetime
from typing import TYPE_CHECKING, Any, Final

import pymysql

from blybot.adapters.toolsdb.store import _target
from blybot.domain.models import Scope
from blybot.domain.ports import StorageError
from blybot.domain.subscriptions import Subscription
from blybot.observability import log_event
from blybot.services.actions import parse_schedule

if TYPE_CHECKING:
    from blybot.adapters.toolsdb.store import SqlRunner

# Two string identities per row: the DM ``(dm_platform, dm_channel)`` a
# subscription belongs to (no thread — a DM has no topic), and the group
# ``(platform, channel, thread)`` it summarizes. The legacy telegram ints
# (dm_chat_id / chat_id / thread_id) are kept NULLABLE and dual-written so
# the prior release still reads every row on a rollback. A fresh table is
# created in this final shape and skips every migration below.
SUBSCRIPTIONS_SCHEMA: Final = """
CREATE TABLE IF NOT EXISTS subscriptions (
    sub_id      VARCHAR(16)  NOT NULL,
    dm_platform VARCHAR(32)  NOT NULL DEFAULT 'telegram',
    dm_channel  VARCHAR(190) NOT NULL DEFAULT '',
    platform    VARCHAR(32)  NOT NULL DEFAULT 'telegram',
    channel     VARCHAR(190) NOT NULL DEFAULT '',
    thread      VARCHAR(190) NOT NULL DEFAULT '',
    dm_chat_id  BIGINT       NULL,
    chat_id     BIGINT       NULL,
    thread_id   BIGINT       NULL,
    schedule    VARCHAR(32)  NOT NULL,
    recipe      VARCHAR(32)  NOT NULL,
    lang        VARCHAR(8)   NOT NULL,
    last_run    VARCHAR(32)  NULL,
    PRIMARY KEY (sub_id),
    KEY by_user (dm_platform, dm_channel)
)
"""

_COLUMNS: Final = (
    "sub_id, dm_platform, dm_channel, platform, channel, thread, schedule, recipe, lang, last_run"
)
# Dual-write: both string identities and the legacy ints are stored.
Q_ADD: Final = """
INSERT INTO subscriptions
    (sub_id, dm_platform, dm_channel, platform, channel, thread,
     dm_chat_id, chat_id, thread_id, schedule, recipe, lang, last_run)
VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
"""
Q_OWNED: Final = (
    "SELECT sub_id FROM subscriptions WHERE sub_id = %s AND dm_platform = %s AND dm_channel = %s"
)
Q_DELETE: Final = (
    "DELETE FROM subscriptions WHERE sub_id = %s AND dm_platform = %s AND dm_channel = %s"
)
Q_LIST_FOR_USER: Final = (
    f"SELECT {_COLUMNS} FROM subscriptions "  # noqa: S608
    "WHERE dm_platform = %s AND dm_channel = %s ORDER BY sub_id"
)
Q_LIST_ALL: Final = f"SELECT {_COLUMNS} FROM subscriptions ORDER BY sub_id"  # noqa: S608
Q_STAMP: Final = "UPDATE subscriptions SET last_run = %s WHERE sub_id = %s"
# Follow a group→supergroup upgrade: sub_id is the PK, so re-keying the
# group channel never collides and needs no collision-clear (unlike
# profiles/messages). The dual-written chat_id moves in lockstep.
Q_MIGRATE: Final = (
    "UPDATE subscriptions SET channel = %s, chat_id = %s WHERE platform = %s AND channel = %s"
)

# Idempotent in-place upgrade for tables that predate the string identity
# (scope PR-2). Each ADD no-ops once applied; the backfills touch only
# un-backfilled rows.
SUBS_ADD_DM_PLATFORM: Final = (
    "ALTER TABLE subscriptions "
    "ADD COLUMN IF NOT EXISTS dm_platform VARCHAR(32) NOT NULL DEFAULT 'telegram'"
)
SUBS_ADD_DM_CHANNEL: Final = (
    "ALTER TABLE subscriptions ADD COLUMN IF NOT EXISTS dm_channel VARCHAR(190) NOT NULL DEFAULT ''"
)
SUBS_ADD_PLATFORM: Final = (
    "ALTER TABLE subscriptions "
    "ADD COLUMN IF NOT EXISTS platform VARCHAR(32) NOT NULL DEFAULT 'telegram'"
)
SUBS_ADD_CHANNEL: Final = (
    "ALTER TABLE subscriptions ADD COLUMN IF NOT EXISTS channel VARCHAR(190) NOT NULL DEFAULT ''"
)
SUBS_ADD_THREAD: Final = (
    "ALTER TABLE subscriptions ADD COLUMN IF NOT EXISTS thread VARCHAR(190) NOT NULL DEFAULT ''"
)
SUBS_BACKFILL_SCOPE: Final = (
    "UPDATE subscriptions SET channel = CAST(chat_id AS CHAR), "
    "thread = IF(thread_id = 0, '', CAST(thread_id AS CHAR)) "
    "WHERE channel = '' AND chat_id IS NOT NULL"
)
# The DM has no thread, so only dm_channel is derived from dm_chat_id.
SUBS_BACKFILL_DM: Final = (
    "UPDATE subscriptions SET dm_channel = CAST(dm_chat_id AS CHAR) "
    "WHERE dm_channel = '' AND dm_chat_id IS NOT NULL"
)
# The PK is sub_id (unchanged); instead rebuild the by_user secondary index
# from (dm_chat_id) onto the string DM identity. Guarded so it runs once:
# on an old table dm_channel is not yet a by_user column.
Q_DM_CHANNEL_IN_BY_USER: Final = """
SELECT COUNT(*) FROM information_schema.STATISTICS
WHERE table_schema = DATABASE() AND table_name = 'subscriptions'
  AND index_name = 'by_user' AND column_name = 'dm_channel'
"""
SUBS_REBUILD_BY_USER: Final = (
    "ALTER TABLE subscriptions DROP INDEX by_user, ADD INDEX by_user (dm_platform, dm_channel)"
)
# Relax the ints to nullable so a future non-telegram row can leave them
# NULL. None sit in the primary key (sub_id does), so ordering versus the
# index rebuild is immaterial. Guarded to run once.
Q_CHAT_ID_NULLABLE: Final = """
SELECT IS_NULLABLE FROM information_schema.COLUMNS
WHERE table_schema = DATABASE() AND table_name = 'subscriptions'
  AND column_name = 'chat_id'
"""
SUBS_CHAT_ID_NULLABLE: Final = "ALTER TABLE subscriptions MODIFY chat_id BIGINT NULL"
SUBS_THREAD_ID_NULLABLE: Final = "ALTER TABLE subscriptions MODIFY thread_id BIGINT NULL"
SUBS_DM_CHAT_ID_NULLABLE: Final = "ALTER TABLE subscriptions MODIFY dm_chat_id BIGINT NULL"


class ToolsDbSubscriptions:
    """SubscriptionStore backed by the ``subscriptions`` table."""

    def __init__(self, runner: SqlRunner) -> None:
        self._runner = runner

    async def bootstrap(self) -> None:
        """Create the table, then migrate an older one in place.

        Idempotent: a fresh table is complete from ``SUBSCRIPTIONS_SCHEMA``;
        an older int-keyed table gains the DM and group string identity
        columns, has every row backfilled, its ``by_user`` index rebuilt
        onto the string DM identity, and its ints relaxed to nullable —
        dropping no rows. Every step no-ops once applied.
        """
        await self._run(SUBSCRIPTIONS_SCHEMA, ())
        await self._run(SUBS_ADD_DM_PLATFORM, ())
        await self._run(SUBS_ADD_DM_CHANNEL, ())
        await self._run(SUBS_ADD_PLATFORM, ())
        await self._run(SUBS_ADD_CHANNEL, ())
        await self._run(SUBS_ADD_THREAD, ())
        await self._run(SUBS_BACKFILL_SCOPE, ())
        await self._run(SUBS_BACKFILL_DM, ())
        rows = await self._run(Q_DM_CHANNEL_IN_BY_USER, ())
        if rows and not int(rows[0][0]):
            await self._run(SUBS_REBUILD_BY_USER, ())
            log_event("subscriptions_migrated", "ok")
        rows = await self._run(Q_CHAT_ID_NULLABLE, ())
        if rows and rows[0][0] == "NO":
            await self._run(SUBS_CHAT_ID_NULLABLE, ())
            await self._run(SUBS_THREAD_ID_NULLABLE, ())
            await self._run(SUBS_DM_CHAT_ID_NULLABLE, ())
            log_event("subscriptions_migrated", "ok")

    async def add(self, subscription: Subscription) -> None:
        """Persist a new subscription."""
        last_run = subscription.last_run.isoformat() if subscription.last_run else None
        chat_id, thread_id = _target(subscription.scope)
        await self._run(
            Q_ADD,
            (
                subscription.sub_id,
                subscription.dm.platform,
                subscription.dm.channel,
                subscription.scope.platform,
                subscription.scope.channel,
                subscription.scope.thread,
                int(subscription.dm.channel),
                chat_id,
                thread_id,
                subscription.schedule.token,
                subscription.recipe,
                subscription.lang,
                last_run,
            ),
        )

    async def remove(self, dm: Scope, sub_id: str) -> bool:
        """Delete the caller's subscription; return whether one existed.

        Scoped to the DM scope so a user can only remove their own —
        guessing another user's ``sub_id`` deletes nothing.
        """
        if not await self._run(Q_OWNED, (sub_id, dm.platform, dm.channel)):
            return False
        await self._run(Q_DELETE, (sub_id, dm.platform, dm.channel))
        return True

    async def list_for_user(self, dm: Scope) -> list[Subscription]:
        """Return every subscription this DM scope owns, oldest first."""
        rows = await self._run(Q_LIST_FOR_USER, (dm.platform, dm.channel))
        return [_subscription_from_row(row) for row in rows]

    async def list_all(self) -> list[Subscription]:
        """Return every subscription (for the digest scheduler)."""
        rows = await self._run(Q_LIST_ALL, ())
        return [_subscription_from_row(row) for row in rows]

    async def stamp(self, sub_id: str, last_run: datetime) -> None:
        """Advance a subscription's ``last_run`` watermark durably."""
        await self._run(Q_STAMP, (last_run.isoformat(), sub_id))

    async def migrate(self, old: Scope, new: Scope) -> None:
        """Re-key a group's subscriptions after a group→supergroup upgrade."""
        await self._run_tx(
            [(Q_MIGRATE, (new.channel, int(new.channel), old.platform, old.channel))]
        )

    async def _run(self, query: str, params: tuple[Any, ...]) -> list[tuple[Any, ...]]:
        try:
            return await asyncio.to_thread(self._runner.run, query, params)
        except (pymysql.MySQLError, OSError, KeyError) as error:
            log_event("subscriptions", "error")
            msg = "subscription store unavailable"
            raise StorageError(msg) from error

    async def _run_tx(self, statements: list[tuple[str, tuple[Any, ...]]]) -> None:
        try:
            await asyncio.to_thread(self._runner.run_tx, statements)
        except (pymysql.MySQLError, OSError, KeyError) as error:
            log_event("subscriptions", "error")
            msg = "subscription store unavailable"
            raise StorageError(msg) from error


def _subscription_from_row(row: tuple[Any, ...]) -> Subscription:
    sub_id, dm_platform, dm_channel, platform, channel, thread, schedule, recipe, lang, last_run = (
        row
    )
    return Subscription(
        sub_id=sub_id,
        dm=Scope(dm_platform, dm_channel),
        scope=Scope(platform, channel, thread),
        schedule=parse_schedule(schedule),
        recipe=recipe,
        lang=lang,
        last_run=datetime.fromisoformat(last_run) if last_run else None,
    )
