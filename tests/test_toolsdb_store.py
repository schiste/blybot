"""ToolsDbStore tests against a SQL-level fake, plus the PyMySQL runner."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import pymysql
import pytest
from cryptography.fernet import Fernet

from blybot.adapters.toolsdb.store import (
    MIGRATE_ADD_THREAD,
    MIGRATE_REBUILD_PK,
    Q_DELETE,
    Q_GET,
    Q_GET_CURSOR,
    Q_LIST_EVENT_ENABLED,
    Q_MIGRATE,
    Q_MIGRATE_CLEAR,
    Q_SET_CURSOR,
    Q_THREAD_IN_PK,
    Q_UPSERT,
    Q_VAULT_CLEAR,
    Q_VAULT_READ,
    Q_VAULT_WRITE,
    SCHEMA,
    PymysqlRunner,
    ToolsDbStore,
)
from blybot.domain.models import ConsentMode, EventKind, GroupProfile
from blybot.domain.ports import StorageError


class FakeToolsDb:
    """Interprets the store's exact query constants against a dict."""

    def __init__(self) -> None:
        self.tables: dict[tuple[int, int], dict[str, Any]] = {}
        self.thread_in_pk = False  # simulates an old (single-key) table
        self.schema_migrated = False
        self.fail = False
        self.schema_created = False

    def _row(self, key: tuple[int, int]) -> dict[str, Any]:
        return self.tables.setdefault(
            key,
            {
                "log_page": None,
                "repo": None,
                "consent_mode": None,
                "events_enabled": 0,
                "event_kinds": "",
                "token": None,
                "cursor": None,
            },
        )

    def _as_profile_row(self, key: tuple[int, int]) -> tuple[Any, ...]:
        chat_id, thread_id = key
        row = self.tables[key]
        return (
            chat_id,
            thread_id,
            row["log_page"],
            row["repo"],
            row["consent_mode"],
            row["events_enabled"],
            row["event_kinds"],
            row["token"] is not None,
        )

    def run(self, query: str, params: tuple[Any, ...]) -> list[tuple[Any, ...]]:
        if self.fail:
            msg = "db down"
            raise OSError(msg)
        # Guard the exact failure the fake would otherwise mask: a SQL
        # constant whose %s count drifts from what the caller passes.
        assert query.count("%s") == len(params), f"placeholder/param mismatch: {query!r}"
        if query == SCHEMA:
            if not self.schema_created:  # CREATE IF NOT EXISTS: no-op on old tables
                self.schema_created = True
                self.thread_in_pk = True  # a freshly created table has the composite key
            return []
        if query == MIGRATE_ADD_THREAD:
            return []  # column add: no-op in the fake
        if query == Q_THREAD_IN_PK:
            return [(1 if self.thread_in_pk else 0,)]
        if query == MIGRATE_REBUILD_PK:
            self.thread_in_pk = True
            self.schema_migrated = True
            return []
        if query == Q_MIGRATE_CLEAR:
            (chat_id,) = params
            for key in [k for k in self.tables if k[0] == chat_id]:
                del self.tables[key]
            return []
        if query == Q_UPSERT:
            chat_id, thread_id, log_page, repo, consent, events_enabled, kinds = params
            row = self._row((chat_id, thread_id))
            row.update(
                log_page=log_page,
                repo=repo,
                consent_mode=consent,
                events_enabled=events_enabled,
                event_kinds=kinds,
            )
            return []
        if query == Q_GET:
            key = (params[0], params[1])
            return [self._as_profile_row(key)] if key in self.tables else []
        if query == Q_LIST_EVENT_ENABLED:
            return [
                self._as_profile_row(key)
                for key, row in self.tables.items()
                if row["events_enabled"]
            ]
        if query == Q_DELETE:
            self.tables.pop((params[0], params[1]), None)
            return []
        if query == Q_GET_CURSOR:
            key = (params[0], params[1])
            return [(self.tables[key]["cursor"],)] if key in self.tables else []
        if query == Q_SET_CURSOR:
            cursor, chat_id, thread_id, repo = params
            key = (chat_id, thread_id)
            if key in self.tables and self.tables[key]["repo"] == repo:
                self.tables[key]["cursor"] = cursor
            return []
        if query == Q_MIGRATE:
            new_id, old_id = params
            for chat_id, thread_id in [k for k in self.tables if k[0] == old_id]:
                self.tables[new_id, thread_id] = self.tables.pop((chat_id, thread_id))
            return []
        if query == Q_VAULT_WRITE:
            chat_id, thread_id, ciphertext = params
            self._row((chat_id, thread_id))["token"] = bytes(ciphertext)
            return []
        if query == Q_VAULT_READ:
            key = (params[0], params[1])
            return [(self.tables[key]["token"],)] if key in self.tables else []
        if query == Q_VAULT_CLEAR:
            key = (params[0], params[1])
            if key in self.tables:
                self.tables[key]["token"] = None
            return []
        pytest.fail(f"unexpected query: {query}")


def make_store() -> tuple[ToolsDbStore, FakeToolsDb]:
    fake = FakeToolsDb()
    return ToolsDbStore(runner=fake, fernet_key=Fernet.generate_key().decode()), fake


PROFILE = GroupProfile(
    chat_id=-100500,
    log_page="Telegram logs/Test",
    repo="schiste/blybot",
    consent_mode=ConsentMode.AUTHOR_ONLY,
    events_enabled=True,
    event_kinds=frozenset({EventKind.RELEASES, EventKind.PRS}),
)


async def test_bootstrap_creates_a_fresh_schema_without_migrating() -> None:
    store, fake = make_store()
    await store.bootstrap()
    assert fake.schema_created
    assert not fake.schema_migrated  # fresh table is already up to date


async def test_bootstrap_upgrades_an_old_single_key_table_in_place() -> None:
    store, fake = make_store()
    fake.schema_created = True  # pretend the table predates thread_id...
    fake.thread_in_pk = False  # ...with a single-column primary key
    await store.bootstrap()
    assert fake.schema_migrated  # primary key rebuilt, no data dropped


async def test_migration_into_an_occupied_destination_replaces_it() -> None:
    store, _ = make_store()
    await store.upsert(GroupProfile(chat_id=-1, thread_id=0, log_page="Old/dest"))
    await store.upsert(GroupProfile(chat_id=-9, thread_id=0, log_page="Source"))
    await store.upsert(GroupProfile(chat_id=-9, thread_id=5, log_page="Source topic"))
    await store.migrate(-9, -1)

    assert await store.get(-9, 0) is None  # source moved
    moved = await store.get(-1, 0)
    assert moved is not None
    assert moved.log_page == "Source"  # authoritative source overwrote the dest
    topic = await store.get(-1, 5)
    assert topic is not None
    assert topic.log_page == "Source topic"


async def test_profile_roundtrip() -> None:
    store, _ = make_store()
    await store.upsert(PROFILE)
    loaded = await store.get(-100500, 0)
    assert loaded == PROFILE


async def test_missing_profile_reads_as_none() -> None:
    store, _ = make_store()
    assert await store.get(-1, 0) is None


async def test_delete_forgets_the_group() -> None:
    store, _ = make_store()
    await store.upsert(PROFILE)
    await store.delete(-100500, 0)
    assert await store.get(-100500, 0) is None


async def test_list_event_enabled_filters() -> None:
    store, _ = make_store()
    await store.upsert(PROFILE)
    quiet = GroupProfile(chat_id=-2, events_enabled=False)
    await store.upsert(quiet)
    enabled = await store.list_event_enabled()
    assert [profile.chat_id for profile in enabled] == [-100500]


async def test_cursor_roundtrip_and_default() -> None:
    store, _ = make_store()
    await store.upsert(PROFILE)
    assert await store.get_cursor(-100500, 0) is None
    await store.set_cursor(-100500, 0, 'W/"etag123"', PROFILE.repo or "")
    assert await store.get_cursor(-100500, 0) == 'W/"etag123"'


async def test_tokens_are_encrypted_at_rest_and_roundtrip() -> None:
    store, fake = make_store()
    await store.store_token(-100500, 0, "ghp_secret")

    stored = fake.tables[-100500, 0]["token"]
    assert b"ghp_secret" not in stored  # ciphertext only in the database
    assert await store.fetch_token(-100500, 0) == "ghp_secret"

    profile = await store.get(-100500, 0)
    assert profile is not None
    assert profile.has_token


async def test_upsert_preserves_token_and_cursor() -> None:
    store, _ = make_store()
    await store.upsert(PROFILE)  # the row must carry the repo the cursor is for
    await store.store_token(-100500, 0, "ghp_secret")
    await store.set_cursor(-100500, 0, "etag", "schiste/blybot")
    await store.upsert(PROFILE)
    assert await store.fetch_token(-100500, 0) == "ghp_secret"
    assert await store.get_cursor(-100500, 0) == "etag"


async def test_token_absent_reads_as_none() -> None:
    store, _ = make_store()
    await store.upsert(PROFILE)
    assert await store.fetch_token(-100500, 0) is None
    assert await store.fetch_token(-999, 0) is None


async def test_delete_token_only_clears_the_token() -> None:
    store, _ = make_store()
    await store.upsert(PROFILE)
    await store.store_token(-100500, 0, "ghp_secret")
    await store.delete_token(-100500, 0)
    assert await store.fetch_token(-100500, 0) is None
    assert await store.get(-100500, 0) == PROFILE


async def test_rotated_key_reads_as_no_token_and_logs(
    caplog: pytest.LogCaptureFixture,
) -> None:
    fake = FakeToolsDb()
    old = ToolsDbStore(runner=fake, fernet_key=Fernet.generate_key().decode())
    await old.store_token(-1, 0, "ghp_secret")
    rotated = ToolsDbStore(runner=fake, fernet_key=Fernet.generate_key().decode())
    with caplog.at_level(logging.INFO, logger="blybot"):
        assert await rotated.fetch_token(-1, 0) is None
    assert any("token_vault" in message for message in caplog.messages)


async def test_database_failure_raises_storage_error() -> None:
    store, fake = make_store()
    fake.fail = True
    with pytest.raises(StorageError):
        await store.get(-1, 0)


def test_pymysql_runner_connects_with_cnf_credentials(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cnf = tmp_path / "replica.my.cnf"
    cnf.write_text("[client]\nuser='s12345'\npassword='hunter2'\n")
    seen: dict[str, Any] = {}

    class FakeCursor:
        def __enter__(self) -> FakeCursor:
            return self

        def __exit__(self, *args: object) -> None:
            return None

        def execute(self, query: str, params: tuple[Any, ...]) -> None:
            seen["query"], seen["params"] = query, params

        def fetchall(self) -> list[tuple[Any, ...]]:
            return [(1,)]

    class FakeConnection:
        def cursor(self) -> FakeCursor:
            return FakeCursor()

        def close(self) -> None:
            seen["closed"] = True

    def fake_connect(**kwargs: Any) -> FakeConnection:
        seen.update(kwargs)
        return FakeConnection()

    monkeypatch.setattr(pymysql, "connect", fake_connect)

    runner = PymysqlRunner(host="tools.db.svc.wikimedia.cloud", database="", cnf_path=cnf)
    rows = runner.run("SELECT 1", ())

    assert rows == [(1,)]
    assert seen["user"] == "s12345"
    assert seen["password"] == "hunter2"  # noqa: S105
    assert seen["database"] == "s12345__blybot"  # derived from the cnf user
    assert seen["closed"] is True

    explicit = PymysqlRunner(host="h", database="custom__db", cnf_path=cnf)
    explicit.run("SELECT 1", ())
    assert seen["database"] == "custom__db"


async def test_cursor_writes_are_repo_guarded() -> None:
    """An in-flight cursor for the OLD binding never lands on a new one."""
    store, _ = make_store()
    await store.upsert(PROFILE)
    await store.set_cursor(-100500, 0, "stale", "some/other")  # repo mismatch
    assert await store.get_cursor(-100500, 0) is None


async def test_migrate_rekeys_the_profile() -> None:
    store, _ = make_store()
    await store.upsert(PROFILE)
    await store.migrate(-100500, -200600)
    assert await store.get(-100500, 0) is None
    migrated = await store.get(-200600, 0)
    assert migrated is not None
    assert migrated.repo == PROFILE.repo
