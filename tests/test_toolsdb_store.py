"""ToolsDbStore tests against a SQL-level fake, plus the PyMySQL runner."""

from __future__ import annotations

import logging
from dataclasses import replace
from pathlib import Path
from typing import Any

import pymysql
import pytest
from cryptography.fernet import Fernet

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
    PymysqlRunner,
    ToolsDbStore,
)
from blybot.domain.models import ConsentMode, GroupProfile, LlmSettings, Scope
from blybot.domain.ports import StorageError
from blybot.services.actions import parse_action
from blybot.services.rules import parse_rule


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


def make_store() -> tuple[ToolsDbStore, FakeToolsDb]:
    fake = FakeToolsDb()
    return ToolsDbStore(runner=fake, fernet_key=Fernet.generate_key().decode()), fake


PROFILE = GroupProfile(
    scope=Scope("telegram", "-100500"),
    log_page="Telegram logs/Test",
    repo="schiste/blybot",
    consent_mode=ConsentMode.AUTHOR_ONLY,
    events_enabled=True,
)


async def test_bootstrap_creates_a_fresh_schema_without_migrating() -> None:
    store, fake = make_store()
    await store.bootstrap()
    assert fake.schema_created
    assert not fake.schema_migrated  # fresh table is already up to date


async def test_bootstrap_upgrades_an_old_int_keyed_table_in_place() -> None:
    store, fake = make_store()
    fake.schema_created = True  # pretend the table predates the string identity...
    fake.channel_in_pk = False  # ...keyed on (chat_id, thread_id)
    fake.chat_id_nullable = False
    await store.bootstrap()
    assert fake.schema_migrated  # string PK rebuilt + ints relaxed, no data dropped
    assert fake.channel_in_pk  # channel is now the primary key
    assert fake.chat_id_nullable  # ...and the ints may be NULL for future platforms


async def test_bootstrap_backfills_a_legacy_int_only_row_and_is_idempotent() -> None:
    """A pre-existing row with ONLY the ints set is readable via the string key."""
    store, fake = make_store()
    fake.schema_created = True  # an old table...
    fake.channel_in_pk = False  # ...still keyed on the ints, capture NOT NULL
    fake.chat_id_nullable = False
    fake.seed_legacy(-100500, log_page="Legacy", events_enabled=1)
    fake.seed_legacy(-100500, thread_id=7, log_page="Legacy topic")

    await store.bootstrap()
    await store.bootstrap()  # second pass must be a no-op (backfill guard holds)

    loaded = await store.get(Scope("telegram", "-100500"))
    assert loaded is not None
    assert loaded.log_page == "Legacy"  # the string key resolves the pre-existing row
    topic = await store.get(Scope("telegram", "-100500", "7"))
    assert topic is not None
    assert topic.log_page == "Legacy topic"  # thread_id 7 backfilled to thread "7"
    listed = await store.list_event_enabled()
    assert [p.scope for p in listed] == [Scope("telegram", "-100500")]


async def test_upsert_dual_writes_both_identities() -> None:
    """A freshly upserted row carries the string identity AND the legacy ints."""
    store, fake = make_store()
    await store.upsert(replace(PROFILE, scope=Scope("telegram", "-100500", "7")))
    row = fake._find("telegram", "-100500", "7")
    assert row is not None
    assert (row["platform"], row["channel"], row["thread"]) == ("telegram", "-100500", "7")
    assert (row["chat_id"], row["thread_id"]) == (-100500, 7)  # ints dual-written for rollback


async def test_bootstrap_converts_the_capture_column_to_tri_state_once() -> None:
    store, fake = make_store()
    fake.schema_created = True  # a table from the NOT-NULL-DEFAULT-0 era
    fake.channel_in_pk = False
    fake.chat_id_nullable = False
    fake.seed_legacy(-1, capture_enabled=0)  # "never decided" back then
    fake.seed_legacy(-2, capture_enabled=1)

    await store.bootstrap()

    assert fake.capture_nullable
    profile = await store.get(Scope("telegram", "-1"))
    assert profile is not None
    assert profile.capture_enabled is None  # 0 became "unset", not "off"
    enabled = await store.get(Scope("telegram", "-2"))
    assert enabled is not None
    assert enabled.capture_enabled is True

    # Post-conversion, an explicit off survives later restarts.
    await store.upsert(replace(profile, capture_enabled=False))
    await store.bootstrap()
    decided = await store.get(Scope("telegram", "-1"))
    assert decided is not None
    assert decided.capture_enabled is False


async def test_migration_into_an_occupied_destination_replaces_it() -> None:
    store, _ = make_store()
    await store.upsert(GroupProfile(scope=Scope("telegram", "-1"), log_page="Old/dest"))
    await store.upsert(GroupProfile(scope=Scope("telegram", "-9"), log_page="Source"))
    await store.upsert(GroupProfile(scope=Scope("telegram", "-9", "5"), log_page="Source topic"))
    await store.migrate(Scope("telegram", "-9"), Scope("telegram", "-1"))

    assert await store.get(Scope("telegram", "-9")) is None  # source moved
    moved = await store.get(Scope("telegram", "-1"))
    assert moved is not None
    assert moved.log_page == "Source"  # authoritative source overwrote the dest
    topic = await store.get(Scope("telegram", "-1", "5"))
    assert topic is not None
    assert topic.log_page == "Source topic"


async def test_profile_roundtrip() -> None:
    store, _ = make_store()
    await store.upsert(PROFILE)
    loaded = await store.get(Scope("telegram", "-100500"))
    assert loaded == PROFILE


async def test_rules_roundtrip_through_json_column() -> None:
    store, _ = make_store()
    rules = (
        parse_rule("pr.merged base:main label:release,hotfix digest"),
        parse_rule("issue.opened title:/^WIP:/ author:octocat"),
    )
    await store.upsert(replace(PROFILE, rules=rules))
    loaded = await store.get(Scope("telegram", "-100500"))
    assert loaded is not None
    assert loaded.rules == rules  # ids, triggers, filters, modes all survive


async def test_missing_profile_reads_as_none() -> None:
    store, _ = make_store()
    assert await store.get(Scope("telegram", "-1")) is None


async def test_delete_forgets_the_group() -> None:
    store, _ = make_store()
    await store.upsert(PROFILE)
    await store.delete(Scope("telegram", "-100500"))
    assert await store.get(Scope("telegram", "-100500")) is None


async def test_list_event_enabled_filters() -> None:
    store, _ = make_store()
    await store.upsert(PROFILE)
    quiet = GroupProfile(scope=Scope("telegram", "-2"), events_enabled=False)
    await store.upsert(quiet)
    enabled = await store.list_event_enabled()
    assert [int(profile.scope.channel) for profile in enabled] == [-100500]


def test_scan_queries_request_a_stable_order() -> None:
    """The scheduler/notifier rotation relies on a deterministic scan order."""
    for query in (Q_ACTIONS_LIST, Q_LIST_EVENT_ENABLED, Q_LIST_CAPTURE_ENABLED):
        assert query.rstrip().endswith("ORDER BY platform, channel, thread")


async def test_subscribe_code_round_trips_and_resolves() -> None:
    store, _ = make_store()
    await store.upsert(GroupProfile(scope=Scope("telegram", "-7"), subscribe_code="abc123"))

    got = await store.get(Scope("telegram", "-7"))
    assert got is not None
    assert got.subscribe_code == "abc123"

    by_code = await store.get_by_subscribe_code("abc123")
    assert by_code is not None
    assert int(by_code.scope.channel) == -7
    assert await store.get_by_subscribe_code("nope") is None


async def test_cursors_roundtrip_and_default() -> None:
    store, _ = make_store()
    await store.upsert(PROFILE)
    assert await store.get_cursors(Scope("telegram", "-100500")) == {}
    cursors = {"issues": "2026-07-05T00:00:00Z", "pulls": "2026-07-06T00:00:00Z"}
    await store.set_cursors(Scope("telegram", "-100500"), cursors, PROFILE.repo or "")
    assert await store.get_cursors(Scope("telegram", "-100500")) == cursors


async def test_tokens_are_encrypted_at_rest_and_roundtrip() -> None:
    store, fake = make_store()
    await store.store_token(Scope("telegram", "-100500"), "ghp_secret")

    row = fake._find("telegram", "-100500", "")
    assert row is not None
    stored = row["token"]
    assert b"ghp_secret" not in stored  # ciphertext only in the database
    assert await store.fetch_token(Scope("telegram", "-100500")) == "ghp_secret"

    profile = await store.get(Scope("telegram", "-100500"))
    assert profile is not None
    assert profile.has_token


async def test_upsert_preserves_token_and_cursors() -> None:
    store, _ = make_store()
    await store.upsert(PROFILE)  # the row must carry the repo the cursors are for
    await store.store_token(Scope("telegram", "-100500"), "ghp_secret")
    await store.set_cursors(
        Scope("telegram", "-100500"), {"issues": "2026-07-05T00:00:00Z"}, "schiste/blybot"
    )
    await store.upsert(PROFILE)
    assert await store.fetch_token(Scope("telegram", "-100500")) == "ghp_secret"
    assert await store.get_cursors(Scope("telegram", "-100500")) == {
        "issues": "2026-07-05T00:00:00Z"
    }


async def test_token_absent_reads_as_none() -> None:
    store, _ = make_store()
    await store.upsert(PROFILE)
    assert await store.fetch_token(Scope("telegram", "-100500")) is None
    assert await store.fetch_token(Scope("telegram", "-999")) is None


async def test_delete_token_only_clears_the_token() -> None:
    store, _ = make_store()
    await store.upsert(PROFILE)
    await store.store_token(Scope("telegram", "-100500"), "ghp_secret")
    await store.delete_token(Scope("telegram", "-100500"))
    assert await store.fetch_token(Scope("telegram", "-100500")) is None
    assert await store.get(Scope("telegram", "-100500")) == PROFILE


async def test_rotated_key_reads_as_no_token_and_logs(
    caplog: pytest.LogCaptureFixture,
) -> None:
    fake = FakeToolsDb()
    old = ToolsDbStore(runner=fake, fernet_key=Fernet.generate_key().decode())
    await old.store_token(Scope("telegram", "-1"), "ghp_secret")
    rotated = ToolsDbStore(runner=fake, fernet_key=Fernet.generate_key().decode())
    with caplog.at_level(logging.INFO, logger="blybot"):
        assert await rotated.fetch_token(Scope("telegram", "-1")) is None
    assert any("token_vault" in message for message in caplog.messages)


async def test_actions_roundtrip_through_json_column() -> None:
    db = FakeToolsDb()
    store = ToolsDbStore(runner=db, fernet_key=Fernet.generate_key().decode())
    spec = parse_action("daily@06:00 summarize model=large", now_iso="2026-07-25T12:00:00+00:00")

    await store.set_actions(Scope("telegram", "-1"), (spec,))

    assert await store.get_actions(Scope("telegram", "-1")) == (spec,)
    assert await store.list_scheduled() == [(Scope("telegram", "-1"), (spec,))]


async def test_actions_default_to_empty_and_empty_lists_are_not_scheduled() -> None:
    db = FakeToolsDb()
    store = ToolsDbStore(runner=db, fernet_key=Fernet.generate_key().decode())

    assert await store.get_actions(Scope("telegram", "-1")) == ()  # unconfigured scope

    await store.set_actions(Scope("telegram", "-1"), ())  # explicit empty: stored, never polled
    assert await store.get_actions(Scope("telegram", "-1")) == ()
    assert await store.list_scheduled() == []


async def test_database_failure_raises_storage_error() -> None:
    store, fake = make_store()
    fake.fail = True
    with pytest.raises(StorageError):
        await store.get(Scope("telegram", "-1"))
    with pytest.raises(StorageError):
        await store.migrate(
            Scope("telegram", "-1"), Scope("telegram", "-2")
        )  # the transactional path degrades the same way


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


def _tx_connection(events: list[str], *, fail: bool) -> tuple[type, list[str]]:
    executed: list[str] = []

    class FakeCursor:
        def __enter__(self) -> FakeCursor:
            return self

        def __exit__(self, *args: object) -> None:
            return None

        def execute(self, query: str, params: tuple[Any, ...]) -> None:
            del params
            executed.append(query)
            if fail:
                msg = "boom"
                raise RuntimeError(msg)

    class FakeConnection:
        def cursor(self) -> FakeCursor:
            return FakeCursor()

        def commit(self) -> None:
            events.append("commit")

        def rollback(self) -> None:
            events.append("rollback")

        def close(self) -> None:
            events.append("close")

    return FakeConnection, executed


def test_run_tx_commits_all_statements(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cnf = tmp_path / "replica.my.cnf"
    cnf.write_text("[client]\nuser='s12345'\npassword='hunter2'\n")
    events: list[str] = []
    connection_cls, executed = _tx_connection(events, fail=False)
    seen: dict[str, Any] = {}

    def fake_connect(**kwargs: Any) -> Any:
        seen.update(kwargs)
        return connection_cls()

    monkeypatch.setattr(pymysql, "connect", fake_connect)
    runner = PymysqlRunner(host="h", database="d", cnf_path=cnf)

    runner.run_tx([("DELETE 1", (1,)), ("UPDATE 2", (2, 3))])

    assert seen["autocommit"] is False  # one transaction, not autocommit-per-statement
    assert executed == ["DELETE 1", "UPDATE 2"]
    assert events == ["commit", "close"]


def test_run_tx_rolls_back_on_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cnf = tmp_path / "replica.my.cnf"
    cnf.write_text("[client]\nuser='s12345'\npassword='hunter2'\n")
    events: list[str] = []
    connection_cls, _executed = _tx_connection(events, fail=True)
    monkeypatch.setattr(pymysql, "connect", lambda **_kwargs: connection_cls())
    runner = PymysqlRunner(host="h", database="d", cnf_path=cnf)

    with pytest.raises(RuntimeError, match="boom"):
        runner.run_tx([("DELETE 1", (1,))])

    assert events == ["rollback", "close"]  # partial work is undone, never committed


async def test_cursor_writes_are_repo_guarded() -> None:
    """An in-flight cursor map for the OLD binding never lands on a new one."""
    store, _ = make_store()
    await store.upsert(PROFILE)
    await store.set_cursors(
        Scope("telegram", "-100500"), {"issues": "stale"}, "some/other"
    )  # repo mismatch
    assert await store.get_cursors(Scope("telegram", "-100500")) == {}


async def test_migrate_rekeys_the_profile() -> None:
    store, _ = make_store()
    await store.upsert(PROFILE)
    await store.migrate(Scope("telegram", "-100500"), Scope("telegram", "-200600"))
    assert await store.get(Scope("telegram", "-100500")) is None
    migrated = await store.get(Scope("telegram", "-200600"))
    assert migrated is not None
    assert migrated.repo == PROFILE.repo


async def test_llm_settings_roundtrip_through_json_column() -> None:
    store, _fake = make_store()
    profile = replace(PROFILE, llm=LlmSettings(model="large", lang="fr"))

    await store.upsert(profile)
    loaded = await store.get(PROFILE.scope)

    assert loaded is not None
    assert loaded.llm == LlmSettings(model="large", lang="fr")

    await store.upsert(replace(profile, llm=None))
    reloaded = await store.get(PROFILE.scope)
    assert reloaded is not None
    assert reloaded.llm is None


async def test_list_capture_enabled_filters() -> None:
    store, _fake = make_store()
    await store.upsert(replace(PROFILE, capture_enabled=True))
    await store.upsert(GroupProfile(scope=Scope("telegram", "-7")))

    listed = await store.list_capture_enabled()

    assert [int(profile.scope.channel) for profile in listed] == [int(PROFILE.scope.channel)]
