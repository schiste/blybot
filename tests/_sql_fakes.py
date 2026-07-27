"""SQL-level fakes shared by the ToolsDb adapter tests and the conformance suite.

Each fake interprets an adapter's exact query constants against an in-memory
list/dict of rows, modelling the real table's columns, dual-write, and in-place
migration. They back the real ``ToolsDbStore`` / ``ToolsDbArchive`` /
``ToolsDbSubscriptions`` adapters through the ``SqlRunner`` protocol, so a
contract exercised against those adapters drives genuine adapter code — not a
second reimplementation of it.

Relocated here from the three ``test_toolsdb_*.py`` files so the parametrized
conformance contracts can construct the adapters too; the adapter tests import
their fake back from this module.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC
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
    Q_COUNT,
    Q_COUNT_BEFORE,
    Q_PURGE,
    Q_PURGE_BEFORE,
    Q_STORE,
    Q_TOTAL,
    Q_WINDOW,
)
from blybot.adapters.toolsdb.archive import Q_CHANNEL_IN_PK as MSG_Q_CHANNEL_IN_PK
from blybot.adapters.toolsdb.archive import Q_CHAT_ID_NULLABLE as MSG_Q_CHAT_ID_NULLABLE
from blybot.adapters.toolsdb.archive import Q_MIGRATE as MSG_Q_MIGRATE
from blybot.adapters.toolsdb.archive import Q_MIGRATE_CLEAR as MSG_Q_MIGRATE_CLEAR
from blybot.adapters.toolsdb.store import (
    MIGRATE_ADD_ACTIONS,
    MIGRATE_ADD_CAPTURE,
    MIGRATE_ADD_CHANNEL,
    MIGRATE_ADD_CURSORS,
    MIGRATE_ADD_LLM,
    MIGRATE_ADD_PLATFORM,
    MIGRATE_ADD_RULES,
    MIGRATE_ADD_SUBSCRIBE_CODE,
    MIGRATE_ADD_THREAD,
    MIGRATE_ADD_THREAD_STR,
    MIGRATE_BACKFILL_IDENTITY,
    MIGRATE_CAPTURE_NULLABLE,
    MIGRATE_CAPTURE_UNSET,
    MIGRATE_CHAT_ID_NULLABLE,
    MIGRATE_REBUILD_PK,
    MIGRATE_THREAD_ID_NULLABLE,
    Q_ACTIONS_LIST,
    Q_ACTIONS_READ,
    Q_ACTIONS_WRITE,
    Q_CAPTURE_NULLABLE,
    Q_CHANNEL_IN_PK,
    Q_CHAT_ID_NULLABLE,
    Q_DELETE,
    Q_GET,
    Q_GET_BY_CODE,
    Q_GET_CURSORS,
    Q_LIST_CAPTURE_ENABLED,
    Q_LIST_EVENT_ENABLED,
    Q_MIGRATE,
    Q_MIGRATE_CLEAR,
    Q_SET_CURSORS,
    Q_UPSERT,
    Q_VAULT_CLEAR,
    Q_VAULT_READ,
    Q_VAULT_WRITE,
    SCHEMA,
)
from blybot.adapters.toolsdb.subscriptions import (
    Q_ADD,
    Q_DM_CHANNEL_IN_BY_USER,
    Q_LIST_ALL,
    Q_LIST_FOR_USER,
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
)
from blybot.adapters.toolsdb.subscriptions import Q_CHAT_ID_NULLABLE as SUBS_Q_CHAT_ID_NULLABLE
from blybot.adapters.toolsdb.subscriptions import Q_DELETE as Q_DELETE_SUB
from blybot.adapters.toolsdb.subscriptions import Q_MIGRATE as SUBS_Q_MIGRATE


class FakeToolsDb:
    """Interprets the store's exact query constants against a list of rows.

    Rows are plain dicts carrying BOTH the string identity (platform,
    channel, thread) and the legacy ints (chat_id, thread_id), so the fake
    can model dual-write and the in-place migration — backfill, string-PK
    rebuild, nullable relax — the way the real table would.
    """

    _DATA_DEFAULTS: dict[str, Any] = {  # noqa: RUF012 -- copied per row, never mutated in place
        "log_page": None,
        "repo": None,
        "consent_mode": None,
        "events_enabled": 0,
        "capture_enabled": None,
        "rules_json": None,
        "llm_json": None,
        "subscribe_code": None,
        "token": None,
        "cursors": None,
        "actions": None,
    }

    def __init__(self) -> None:
        self.rows: list[dict[str, Any]] = []
        self.channel_in_pk = False  # old table: PK is still (chat_id, thread_id)
        self.chat_id_nullable = False  # old table: the ints are NOT NULL
        self.capture_nullable = False  # old table: NOT NULL tri-state column
        self.schema_migrated = False
        self.fail = False
        self.schema_created = False

    def seed_legacy(self, chat_id: int, thread_id: int = 0, **data: Any) -> dict[str, Any]:
        """Append a pre-migration row: only the ints set, string key blank."""
        row = {
            **self._DATA_DEFAULTS,
            "platform": "telegram",
            "channel": "",
            "thread": "",
            "chat_id": chat_id,
            "thread_id": thread_id,
            **data,
        }
        self.rows.append(row)
        return row

    def _find(self, platform: str, channel: str, thread: str) -> dict[str, Any] | None:
        for row in self.rows:
            if (row["platform"], row["channel"], row["thread"]) == (platform, channel, thread):
                return row
        return None

    def _upsert_row(
        self, platform: str, channel: str, thread: str, chat_id: Any, thread_id: Any
    ) -> dict[str, Any]:
        row = self._find(platform, channel, thread)
        if row is None:
            row = {
                **self._DATA_DEFAULTS,
                "platform": platform,
                "channel": channel,
                "thread": thread,
                "chat_id": chat_id,
                "thread_id": thread_id,
            }
            self.rows.append(row)
        else:  # dual-write keeps the ints in step on every touch
            row["chat_id"], row["thread_id"] = chat_id, thread_id
        return row

    def _sorted(self) -> list[dict[str, Any]]:
        return sorted(self.rows, key=lambda r: (r["platform"], r["channel"], r["thread"]))

    def _as_profile_row(self, row: dict[str, Any]) -> tuple[Any, ...]:
        return (
            row["platform"],
            row["channel"],
            row["thread"],
            row["log_page"],
            row["repo"],
            row["consent_mode"],
            row["events_enabled"],
            row["capture_enabled"],
            row["rules_json"],
            row["llm_json"],
            row["subscribe_code"],
            row["token"] is not None,
        )

    def _run_actions(self, query: str, params: tuple[Any, ...]) -> list[tuple[Any, ...]]:
        """The actions_json queries, kept apart to keep :meth:`run` readable."""
        row: dict[str, Any] | None
        if query == Q_ACTIONS_WRITE:
            platform, channel, thread, chat_id, thread_id, actions_json = params
            row = self._upsert_row(platform, channel, thread, chat_id, thread_id)
            row["actions"] = actions_json
        if query == Q_ACTIONS_READ:
            row = self._find(*params)
            return [(row["actions"],)] if row else []
        if query == Q_ACTIONS_LIST:
            return [
                (row["platform"], row["channel"], row["thread"], row["actions"])
                for row in self._sorted()
                if row["actions"] not in (None, "[]")
            ]
        return []  # MIGRATE_ADD_ACTIONS: no rows

    def _run_capture_migration(self, query: str) -> list[tuple[Any, ...]]:
        """The tri-state capture conversion, kept apart like the actions queries."""
        if query == Q_CAPTURE_NULLABLE:
            return [("YES" if self.capture_nullable else "NO",)]
        if query == MIGRATE_CAPTURE_NULLABLE:
            self.capture_nullable = True
            self.schema_migrated = True
            return []
        for row in self.rows:  # MIGRATE_CAPTURE_UNSET
            if row["capture_enabled"] == 0:
                row["capture_enabled"] = None
        return []

    def run_tx(self, statements: list[tuple[str, tuple[Any, ...]]]) -> None:
        for query, params in statements:
            self.run(query, params)

    def _run_migration(self, query: str) -> list[tuple[Any, ...]] | None:
        """Handle schema/migration statements; return None for data queries."""
        if query == SCHEMA:
            if not self.schema_created:  # CREATE IF NOT EXISTS: no-op on old tables
                self.schema_created = True
                self.channel_in_pk = True  # a fresh table already has the string PK...
                self.chat_id_nullable = True  # ...nullable ints...
                self.capture_nullable = True  # ...and the nullable tri-state column
            return []
        if query in (
            MIGRATE_ADD_THREAD,
            MIGRATE_ADD_RULES,
            MIGRATE_ADD_CURSORS,
            MIGRATE_ADD_CAPTURE,
            MIGRATE_ADD_LLM,
            MIGRATE_ADD_SUBSCRIBE_CODE,
            MIGRATE_ADD_PLATFORM,
            MIGRATE_ADD_CHANNEL,
            MIGRATE_ADD_THREAD_STR,
        ):
            return []  # column add: no-op (columns always present in the fake row)
        if query == MIGRATE_BACKFILL_IDENTITY:
            for row in self.rows:
                if row["channel"] == "" and row["chat_id"] is not None:
                    row["channel"] = str(row["chat_id"])
                    row["thread"] = "" if row["thread_id"] == 0 else str(row["thread_id"])
            return []
        if query == Q_CHANNEL_IN_PK:
            return [(1 if self.channel_in_pk else 0,)]
        if query == MIGRATE_REBUILD_PK:
            self.channel_in_pk = True
            self.schema_migrated = True
            return []
        if query == Q_CHAT_ID_NULLABLE:
            return [("YES" if self.chat_id_nullable else "NO",)]
        if query in (MIGRATE_CHAT_ID_NULLABLE, MIGRATE_THREAD_ID_NULLABLE):
            self.chat_id_nullable = True
            self.schema_migrated = True
            return []
        if query in (Q_CAPTURE_NULLABLE, MIGRATE_CAPTURE_NULLABLE, MIGRATE_CAPTURE_UNSET):
            return self._run_capture_migration(query)
        return None

    def run(self, query: str, params: tuple[Any, ...]) -> list[tuple[Any, ...]]:
        if self.fail:
            msg = "db down"
            raise OSError(msg)
        # Guard the exact failure the fake would otherwise mask: a SQL
        # constant whose %s count drifts from what the caller passes.
        assert query.count("%s") == len(params), f"placeholder/param mismatch: {query!r}"
        row: dict[str, Any] | None
        migrated = self._run_migration(query)
        if migrated is not None:
            return migrated
        if query in (MIGRATE_ADD_ACTIONS, Q_ACTIONS_WRITE, Q_ACTIONS_READ, Q_ACTIONS_LIST):
            return self._run_actions(query, params)
        if query == Q_MIGRATE_CLEAR:
            platform, channel = params
            self.rows = [
                r for r in self.rows if (r["platform"], r["channel"]) != (platform, channel)
            ]
            return []
        if query == Q_UPSERT:
            (
                platform,
                channel,
                thread,
                chat_id,
                thread_id,
                log_page,
                repo,
                consent,
                events,
                capture,
                rules,
                llm,
                code,
            ) = params
            row = self._upsert_row(platform, channel, thread, chat_id, thread_id)
            row.update(
                log_page=log_page,
                repo=repo,
                consent_mode=consent,
                events_enabled=events,
                capture_enabled=capture,
                rules_json=rules,
                llm_json=llm,
                subscribe_code=code,
            )
            return []
        if query == Q_GET:
            row = self._find(*params)
            return [self._as_profile_row(row)] if row else []
        if query == Q_GET_BY_CODE:
            (code,) = params
            hits = [r for r in self.rows if r["subscribe_code"] == code]
            return [self._as_profile_row(hits[0])] if hits else []
        if query == Q_LIST_EVENT_ENABLED:
            return [self._as_profile_row(r) for r in self._sorted() if r["events_enabled"]]
        if query == Q_LIST_CAPTURE_ENABLED:
            return [self._as_profile_row(r) for r in self._sorted() if r["capture_enabled"]]
        if query == Q_DELETE:
            row = self._find(*params)
            if row is not None:
                self.rows.remove(row)
            return []
        if query == Q_GET_CURSORS:
            row = self._find(*params)
            return [(row["cursors"],)] if row else []
        if query == Q_SET_CURSORS:
            cursors, platform, channel, thread, repo = params
            row = self._find(platform, channel, thread)
            if row is not None and row["repo"] == repo:
                row["cursors"] = cursors
            return []
        if query == Q_MIGRATE:
            new_channel, new_chat_id, platform, old_channel = params
            for row in self.rows:
                if (row["platform"], row["channel"]) == (platform, old_channel):
                    row["channel"], row["chat_id"] = new_channel, new_chat_id
            return []
        if query in (Q_VAULT_WRITE, Q_VAULT_READ, Q_VAULT_CLEAR):
            return self._run_vault(query, params)
        pytest.fail(f"unexpected query: {query}")

    def _run_vault(self, query: str, params: tuple[Any, ...]) -> list[tuple[Any, ...]]:
        if query == Q_VAULT_WRITE:
            platform, channel, thread, chat_id, thread_id, ciphertext = params
            self._upsert_row(platform, channel, thread, chat_id, thread_id)["token"] = bytes(
                ciphertext
            )
            return []
        if query == Q_VAULT_READ:
            row = self._find(*params)
            return [(row["token"],)] if row else []
        row = self._find(*params)  # Q_VAULT_CLEAR
        if row is not None:
            row["token"] = None
        return []


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
        if query == MSG_Q_CHANNEL_IN_PK:
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
        if query == MSG_Q_CHAT_ID_NULLABLE:
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
        if query == MSG_Q_MIGRATE_CLEAR:
            platform, channel = params
            self.rows = [
                r for r in self.rows if (r["platform"], r["channel"]) != (platform, channel)
            ]
            return []
        if query == MSG_Q_MIGRATE:
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
        if query == Q_DELETE_SUB:
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
        if query == SUBS_Q_MIGRATE:
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
        if query == SUBS_Q_CHAT_ID_NULLABLE:
            return [("YES" if self.chat_id_nullable else "NO",)]
        if query in (SUBS_CHAT_ID_NULLABLE, SUBS_THREAD_ID_NULLABLE, SUBS_DM_CHAT_ID_NULLABLE):
            self.chat_id_nullable = True
            self.schema_migrated = True
            return []
        return None
