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

from blybot.adapters.toolsdb.store import _scope_of, _target
from blybot.domain.ports import StorageError
from blybot.domain.subscriptions import Subscription
from blybot.observability import log_event
from blybot.services.actions import parse_schedule

if TYPE_CHECKING:
    from blybot.adapters.toolsdb.store import SqlRunner
    from blybot.domain.models import Scope

SUBSCRIPTIONS_SCHEMA: Final = """
CREATE TABLE IF NOT EXISTS subscriptions (
    sub_id     VARCHAR(16) NOT NULL,
    dm_chat_id BIGINT      NOT NULL,
    chat_id    BIGINT      NOT NULL,
    thread_id  BIGINT      NOT NULL DEFAULT 0,
    schedule   VARCHAR(32) NOT NULL,
    recipe     VARCHAR(32) NOT NULL,
    lang       VARCHAR(8)  NOT NULL,
    last_run   VARCHAR(32) NULL,
    PRIMARY KEY (sub_id),
    KEY by_user (dm_chat_id)
)
"""

_COLUMNS: Final = "sub_id, dm_chat_id, chat_id, thread_id, schedule, recipe, lang, last_run"
Q_ADD: Final = """
INSERT INTO subscriptions (sub_id, dm_chat_id, chat_id, thread_id, schedule, recipe, lang, last_run)
VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
"""
Q_OWNED: Final = "SELECT sub_id FROM subscriptions WHERE sub_id = %s AND dm_chat_id = %s"
Q_DELETE: Final = "DELETE FROM subscriptions WHERE sub_id = %s AND dm_chat_id = %s"
Q_LIST_FOR_USER: Final = (
    f"SELECT {_COLUMNS} FROM subscriptions WHERE dm_chat_id = %s ORDER BY sub_id"  # noqa: S608
)
Q_LIST_ALL: Final = f"SELECT {_COLUMNS} FROM subscriptions ORDER BY sub_id"  # noqa: S608
Q_STAMP: Final = "UPDATE subscriptions SET last_run = %s WHERE sub_id = %s"
# Follow a group→supergroup upgrade: sub_id is the PK, so re-keying chat_id
# never collides and needs no collision-clear (unlike profiles/messages).
Q_MIGRATE: Final = "UPDATE subscriptions SET chat_id = %s WHERE chat_id = %s"


class ToolsDbSubscriptions:
    """SubscriptionStore backed by the ``subscriptions`` table."""

    def __init__(self, runner: SqlRunner) -> None:
        self._runner = runner

    async def bootstrap(self) -> None:
        """Create the subscriptions table; idempotent, safe on every startup."""
        await self._run(SUBSCRIPTIONS_SCHEMA, ())

    async def add(self, subscription: Subscription) -> None:
        """Persist a new subscription."""
        last_run = subscription.last_run.isoformat() if subscription.last_run else None
        chat_id, thread_id = _target(subscription.scope)
        await self._run(
            Q_ADD,
            (
                subscription.sub_id,
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
        dm_chat_id = int(dm.channel)
        if not await self._run(Q_OWNED, (sub_id, dm_chat_id)):
            return False
        await self._run(Q_DELETE, (sub_id, dm_chat_id))
        return True

    async def list_for_user(self, dm: Scope) -> list[Subscription]:
        """Return every subscription this DM scope owns, oldest first."""
        rows = await self._run(Q_LIST_FOR_USER, (int(dm.channel),))
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
        await self._run_tx([(Q_MIGRATE, (int(new.channel), int(old.channel)))])

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
    sub_id, dm_chat_id, chat_id, thread_id, schedule, recipe, lang, last_run = row
    return Subscription(
        sub_id=sub_id,
        dm=_scope_of(int(dm_chat_id), 0),
        scope=_scope_of(int(chat_id), int(thread_id)),
        schedule=parse_schedule(schedule),
        recipe=recipe,
        lang=lang,
        last_run=datetime.fromisoformat(last_run) if last_run else None,
    )
