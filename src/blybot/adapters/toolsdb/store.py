"""ToolsDB-backed profile store and token vault (spec 11).

Storage is one MariaDB table on Toolforge's ToolsDB (never SQLite on
NFS). The driver is PyMySQL — synchronous and pure Python — executed
via :func:`asyncio.to_thread`; traffic is a handful of admin commands
and poll cursors, far below anything needing an async driver or a
connection pool. Credentials come from the tool's ``replica.my.cnf``.

Group-supplied tokens are Fernet-encrypted before they touch the
database; plaintext exists only in memory on its way in or out.
"""

from __future__ import annotations

import asyncio
import configparser
import json
from typing import TYPE_CHECKING, Any, Final, Protocol

import pymysql
from cryptography.fernet import Fernet, InvalidToken

from blybot.domain.models import ConsentMode, GroupProfile, Scope
from blybot.domain.ports import StorageError
from blybot.observability import log_event
from blybot.services.actions import dumps_actions, loads_actions
from blybot.services.llmconf import dumps_llm, loads_llm
from blybot.services.rules import dumps_rules, loads_rules

if TYPE_CHECKING:
    from pathlib import Path

    from blybot.domain.models import ActionSpec

# Every row carries two identities. The live one is the opaque string key
# ``(platform, channel, thread)`` — the primary key, and the only thing
# WHERE/ORDER clauses touch. The legacy telegram ``(chat_id, thread_id)``
# ints are kept NULLABLE and dual-written so the prior release still reads
# every row after a rollback; a future non-telegram platform leaves them
# NULL. Every scope this adapter sees is on the Telegram platform.
_PLATFORM: Final = "telegram"


def _keys(scope: Scope) -> tuple[str, str, str]:
    """The string identity ``(platform, channel, thread)`` — the live key."""
    return scope.platform, scope.channel, scope.thread


def _target(scope: Scope) -> tuple[int, int]:
    """Split a telegram :class:`Scope` into its legacy ``(chat_id, thread_id)`` pair.

    Retained only for the dual-write of the (now nullable) int columns;
    the string identity from :func:`_keys` is what every read keys on.
    """
    assert scope.platform == _PLATFORM  # noqa: S101 -- single-platform invariant this PR
    return int(scope.channel), int(scope.thread) if scope.thread else 0


# Final shape for a brand-new deployment: string columns present, int
# columns nullable, string primary key. A fresh table therefore skips
# every migration below.
SCHEMA: Final = """
CREATE TABLE IF NOT EXISTS profiles (
    platform VARCHAR(32) NOT NULL DEFAULT 'telegram',
    channel VARCHAR(190) NOT NULL DEFAULT '',
    thread VARCHAR(190) NOT NULL DEFAULT '',
    chat_id BIGINT NULL,
    thread_id BIGINT NULL,
    log_page VARCHAR(255) NULL,
    repo VARCHAR(140) NULL,
    consent_mode VARCHAR(16) NULL,
    events_enabled TINYINT(1) NOT NULL DEFAULT 0,
    capture_enabled TINYINT(1) NULL DEFAULT NULL,
    subscribe_code VARCHAR(32) NULL DEFAULT NULL,
    rules_json TEXT NULL,
    llm_json TEXT NULL,
    cursors_json TEXT NULL,
    token_ciphertext BLOB NULL,
    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    PRIMARY KEY (platform, channel, thread)
)
"""

_PROFILE_COLUMNS: Final = (
    "platform, channel, thread, log_page, repo, consent_mode, events_enabled, "
    "capture_enabled, rules_json, llm_json, subscribe_code, token_ciphertext IS NOT NULL"
)
_KEY: Final = "platform = %s AND channel = %s AND thread = %s"
Q_GET: Final = f"SELECT {_PROFILE_COLUMNS} FROM profiles WHERE {_KEY}"  # noqa: S608
# Resolve a subscribe deep-link code back to its scope (unique random code).
Q_GET_BY_CODE: Final = f"SELECT {_PROFILE_COLUMNS} FROM profiles WHERE subscribe_code = %s"  # noqa: S608
# The ORDER BY makes the scan order stable across ticks, which the
# per-tick rotation in the scheduler/notifier relies on to keep any scope
# above the cap from being permanently starved.
Q_LIST_EVENT_ENABLED: Final = (
    f"SELECT {_PROFILE_COLUMNS} FROM profiles WHERE events_enabled = 1 "  # noqa: S608
    "ORDER BY platform, channel, thread"
)
Q_LIST_CAPTURE_ENABLED: Final = (
    f"SELECT {_PROFILE_COLUMNS} FROM profiles WHERE capture_enabled = 1 "  # noqa: S608
    "ORDER BY platform, channel, thread"
)
# Dual-write: both the string identity and the legacy int columns are set,
# so the prior release still reads every row on a rollback.
Q_UPSERT: Final = """
INSERT INTO profiles
    (platform, channel, thread, chat_id, thread_id, log_page, repo, consent_mode,
     events_enabled, capture_enabled, rules_json, llm_json, subscribe_code)
VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
ON DUPLICATE KEY UPDATE chat_id = VALUES(chat_id), thread_id = VALUES(thread_id),
    log_page = VALUES(log_page), repo = VALUES(repo),
    consent_mode = VALUES(consent_mode), events_enabled = VALUES(events_enabled),
    capture_enabled = VALUES(capture_enabled), rules_json = VALUES(rules_json),
    llm_json = VALUES(llm_json), subscribe_code = VALUES(subscribe_code)
"""
Q_DELETE: Final = f"DELETE FROM profiles WHERE {_KEY}"  # noqa: S608
Q_GET_CURSORS: Final = f"SELECT cursors_json FROM profiles WHERE {_KEY}"  # noqa: S608
Q_SET_CURSORS: Final = f"UPDATE profiles SET cursors_json = %s WHERE {_KEY} AND repo = %s"  # noqa: S608
# Group→supergroup migration re-keys the group's rows to the new channel.
# The migrating group is authoritative, so any pre-existing rows at the
# destination (a channel the bot somehow already knew) are cleared first —
# otherwise the UPDATE would collide on the (platform, channel, thread)
# primary key and silently strand rows. The dual-written chat_id is moved
# in lockstep so the two identities never disagree.
Q_MIGRATE_CLEAR: Final = "DELETE FROM profiles WHERE platform = %s AND channel = %s"
Q_MIGRATE: Final = (
    "UPDATE profiles SET channel = %s, chat_id = %s WHERE platform = %s AND channel = %s"
)

# Idempotent in-place schema upgrade for tables created before the
# thread_id column existed. Runs on every startup; each step no-ops once
# applied, so no data is ever dropped.
MIGRATE_ADD_THREAD: Final = (
    "ALTER TABLE profiles ADD COLUMN IF NOT EXISTS thread_id BIGINT NOT NULL DEFAULT 0"
)
# Composable rules and per-resource poll cursors arrived after the
# original schema; older tables gain the columns in place. No-op once
# applied. (The retired event_kinds/event_cursor columns are simply
# left in place on old tables — unused and harmless.)
MIGRATE_ADD_RULES: Final = "ALTER TABLE profiles ADD COLUMN IF NOT EXISTS rules_json TEXT NULL"
MIGRATE_ADD_CURSORS: Final = "ALTER TABLE profiles ADD COLUMN IF NOT EXISTS cursors_json TEXT NULL"
MIGRATE_ADD_ACTIONS: Final = "ALTER TABLE profiles ADD COLUMN IF NOT EXISTS actions_json TEXT NULL"
MIGRATE_ADD_CAPTURE: Final = (
    "ALTER TABLE profiles ADD COLUMN IF NOT EXISTS capture_enabled TINYINT(1) NULL DEFAULT NULL"
)
# capture_enabled began life NOT NULL DEFAULT 0, which made "never
# decided" indistinguishable from an explicit /capture off — so a topic
# could not opt out of an enabled group (nor inherit cleanly). Older
# tables are converted once: the column becomes nullable and the 0s
# (all "never decided" — the tri-state shipped with capture itself)
# become NULL. Guarded by an information_schema check so explicit offs
# recorded after the conversion are never wiped by a later restart.
Q_CAPTURE_NULLABLE: Final = """
SELECT IS_NULLABLE FROM information_schema.COLUMNS
WHERE table_schema = DATABASE() AND table_name = 'profiles'
  AND column_name = 'capture_enabled'
"""
MIGRATE_CAPTURE_NULLABLE: Final = (
    "ALTER TABLE profiles MODIFY capture_enabled TINYINT(1) NULL DEFAULT NULL"
)
MIGRATE_CAPTURE_UNSET: Final = (
    "UPDATE profiles SET capture_enabled = NULL WHERE capture_enabled = 0"
)
MIGRATE_ADD_LLM: Final = "ALTER TABLE profiles ADD COLUMN IF NOT EXISTS llm_json TEXT NULL"
MIGRATE_ADD_SUBSCRIBE_CODE: Final = (
    "ALTER TABLE profiles ADD COLUMN IF NOT EXISTS subscribe_code VARCHAR(32) NULL DEFAULT NULL"
)
# The opaque string identity (scope PR-2). Added in place on older tables;
# each ADD is a no-op once applied. The DEFAULTs mean every pre-existing
# row lands with platform 'telegram' and empty channel/thread — the
# backfill below then derives channel/thread from the int columns.
MIGRATE_ADD_PLATFORM: Final = (
    "ALTER TABLE profiles ADD COLUMN IF NOT EXISTS platform VARCHAR(32) NOT NULL DEFAULT 'telegram'"
)
MIGRATE_ADD_CHANNEL: Final = (
    "ALTER TABLE profiles ADD COLUMN IF NOT EXISTS channel VARCHAR(190) NOT NULL DEFAULT ''"
)
MIGRATE_ADD_THREAD_STR: Final = (
    "ALTER TABLE profiles ADD COLUMN IF NOT EXISTS thread VARCHAR(190) NOT NULL DEFAULT ''"
)
# Idempotent backfill: touches only un-backfilled rows (channel still '')
# that carry a legacy chat_id. thread_id 0 (the channel default) maps to
# the empty thread, mirroring _target/_keys.
MIGRATE_BACKFILL_IDENTITY: Final = (
    "UPDATE profiles SET channel = CAST(chat_id AS CHAR), "
    "thread = IF(thread_id = 0, '', CAST(thread_id AS CHAR)) "
    "WHERE channel = '' AND chat_id IS NOT NULL"
)
# The string identity becomes the primary key. Guarded so it runs exactly
# once: on an old table channel is not yet a PRIMARY column (count 0); on a
# fresh or already-migrated table it is (count 1) and the rebuild is
# skipped. Must run BEFORE the int columns are made nullable — a PRIMARY
# KEY column cannot be NULL, so chat_id/thread_id have to leave the key
# first. Replaces the retired (chat_id, thread_id) rebuild.
Q_CHANNEL_IN_PK: Final = """
SELECT COUNT(*) FROM information_schema.STATISTICS
WHERE table_schema = DATABASE() AND table_name = 'profiles'
  AND index_name = 'PRIMARY' AND column_name = 'channel'
"""
MIGRATE_REBUILD_PK: Final = (
    "ALTER TABLE profiles DROP PRIMARY KEY, ADD PRIMARY KEY (platform, channel, thread)"
)
# Now that the ints are no longer in the primary key, make them nullable so
# a future non-telegram row can leave them NULL. Guarded by an
# information_schema check so it runs once; after conversion IS_NULLABLE is
# 'YES' and later restarts skip it.
Q_CHAT_ID_NULLABLE: Final = """
SELECT IS_NULLABLE FROM information_schema.COLUMNS
WHERE table_schema = DATABASE() AND table_name = 'profiles'
  AND column_name = 'chat_id'
"""
MIGRATE_CHAT_ID_NULLABLE: Final = "ALTER TABLE profiles MODIFY chat_id BIGINT NULL"
MIGRATE_THREAD_ID_NULLABLE: Final = "ALTER TABLE profiles MODIFY thread_id BIGINT NULL"
Q_ACTIONS_READ: Final = f"SELECT actions_json FROM profiles WHERE {_KEY}"  # noqa: S608
Q_ACTIONS_WRITE: Final = """
INSERT INTO profiles (platform, channel, thread, chat_id, thread_id, actions_json)
VALUES (%s, %s, %s, %s, %s, %s)
ON DUPLICATE KEY UPDATE actions_json = VALUES(actions_json)
"""
# Scopes with an empty stored list ("[]") have no actions to schedule.
Q_ACTIONS_LIST: Final = (
    "SELECT platform, channel, thread, actions_json FROM profiles "
    "WHERE actions_json IS NOT NULL AND actions_json != '[]' "
    "ORDER BY platform, channel, thread"  # stable scan order for the scheduler's rotation
)
Q_VAULT_READ: Final = f"SELECT token_ciphertext FROM profiles WHERE {_KEY}"  # noqa: S608
Q_VAULT_WRITE: Final = """
INSERT INTO profiles (platform, channel, thread, chat_id, thread_id, token_ciphertext)
VALUES (%s, %s, %s, %s, %s, %s)
ON DUPLICATE KEY UPDATE token_ciphertext = VALUES(token_ciphertext)
"""
Q_VAULT_CLEAR: Final = f"UPDATE profiles SET token_ciphertext = NULL WHERE {_KEY}"  # noqa: S608


class SqlRunner(Protocol):
    """Executes SQL synchronously; returns all rows."""

    def run(self, query: str, params: tuple[Any, ...]) -> list[tuple[Any, ...]]:
        """Run ``query`` with ``params``; empty list for writes."""
        ...

    def run_tx(self, statements: list[tuple[str, tuple[Any, ...]]]) -> None:
        """Run several statements in one all-or-nothing transaction."""
        ...


class PymysqlRunner:
    """Connection-per-call runner against ToolsDB.

    A fresh connection per statement sidesteps stale-connection
    handling entirely; at this traffic level the overhead is noise.
    """

    def __init__(self, host: str, database: str, cnf_path: Path) -> None:
        self._host = host
        self._database = database
        self._cnf_path = cnf_path

    def run(self, query: str, params: tuple[Any, ...]) -> list[tuple[Any, ...]]:
        """Run one statement with autocommit; return all rows."""
        user, password = self._credentials()
        connection = pymysql.connect(
            host=self._host,
            user=user,
            password=password,
            database=self._database or f"{user}__blybot",
            autocommit=True,
            connect_timeout=10,
        )
        try:
            with connection.cursor() as cursor:
                cursor.execute(query, params)
                return list(cursor.fetchall())
        finally:
            connection.close()

    def run_tx(self, statements: list[tuple[str, tuple[Any, ...]]]) -> None:
        """Run several statements in one transaction; roll back on any error."""
        user, password = self._credentials()
        connection = pymysql.connect(
            host=self._host,
            user=user,
            password=password,
            database=self._database or f"{user}__blybot",
            autocommit=False,
            connect_timeout=10,
        )
        try:
            with connection.cursor() as cursor:
                for query, params in statements:
                    cursor.execute(query, params)
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _credentials(self) -> tuple[str, str]:
        parser = configparser.ConfigParser()
        parser.read(self._cnf_path)
        client = parser["client"]
        return client["user"].strip("'\""), client["password"].strip("'\"")


class ToolsDbStore:
    """ProfileStore + TokenVault + ActionStore backed by one ToolsDB table."""

    def __init__(self, runner: SqlRunner, fernet_key: str) -> None:
        self._runner = runner
        self._fernet = Fernet(fernet_key)

    async def bootstrap(self) -> None:
        """Create the schema, then bring an older table up to date in place.

        Idempotent: a fresh table is created complete by ``SCHEMA``; an
        older table gains the string identity columns, has every row
        backfilled from its legacy ints, its primary key rebuilt to
        ``(platform, channel, thread)``, and its int columns made nullable
        — without dropping any rows. Every step is a no-op once applied, so
        re-running is safe.
        """
        await self._run(SCHEMA, ())
        await self._run(MIGRATE_ADD_THREAD, ())
        await self._run(MIGRATE_ADD_RULES, ())
        await self._run(MIGRATE_ADD_CURSORS, ())
        await self._run(MIGRATE_ADD_ACTIONS, ())
        await self._run(MIGRATE_ADD_CAPTURE, ())
        await self._run(MIGRATE_ADD_LLM, ())
        await self._run(MIGRATE_ADD_SUBSCRIBE_CODE, ())
        await self._run(MIGRATE_ADD_PLATFORM, ())
        await self._run(MIGRATE_ADD_CHANNEL, ())
        await self._run(MIGRATE_ADD_THREAD_STR, ())
        await self._run(MIGRATE_BACKFILL_IDENTITY, ())
        rows = await self._run(Q_CAPTURE_NULLABLE, ())
        if rows and rows[0][0] == "NO":
            await self._run(MIGRATE_CAPTURE_NULLABLE, ())
            await self._run(MIGRATE_CAPTURE_UNSET, ())
            log_event("storage_migrated", "ok")
        # PK rebuild before the int columns are relaxed to nullable: a
        # PRIMARY KEY column may not be NULL, so the ints must leave the
        # key first.
        rows = await self._run(Q_CHANNEL_IN_PK, ())
        if rows and not int(rows[0][0]):
            await self._run(MIGRATE_REBUILD_PK, ())
            log_event("storage_migrated", "ok")
        rows = await self._run(Q_CHAT_ID_NULLABLE, ())
        if rows and rows[0][0] == "NO":
            await self._run(MIGRATE_CHAT_ID_NULLABLE, ())
            await self._run(MIGRATE_THREAD_ID_NULLABLE, ())
            log_event("storage_migrated", "ok")

    async def get(self, scope: Scope) -> GroupProfile | None:
        """Return the scope's profile, or ``None`` if unconfigured."""
        rows = await self._run(Q_GET, _keys(scope))
        return _profile_from_row(rows[0]) if rows else None

    async def get_by_subscribe_code(self, code: str) -> GroupProfile | None:
        """Return the scope whose subscribe_code matches, or ``None``."""
        rows = await self._run(Q_GET_BY_CODE, (code,))
        return _profile_from_row(rows[0]) if rows else None

    async def upsert(self, profile: GroupProfile) -> None:
        """Create or update the profile (token and cursors are untouched)."""
        platform, channel, thread = _keys(profile.scope)
        chat_id, thread_id = _target(profile.scope)
        await self._run(
            Q_UPSERT,
            (
                platform,
                channel,
                thread,
                chat_id,
                thread_id,
                profile.log_page,
                profile.repo,
                profile.consent_mode.value if profile.consent_mode else None,
                int(profile.events_enabled),
                None if profile.capture_enabled is None else int(profile.capture_enabled),
                dumps_rules(profile.rules),
                dumps_llm(profile.llm) if profile.llm is not None else None,
                profile.subscribe_code,
            ),
        )

    async def delete(self, scope: Scope) -> None:
        """Forget everything about the scope, token and cursor included."""
        await self._run(Q_DELETE, _keys(scope))

    async def list_event_enabled(self) -> list[GroupProfile]:
        """Return every profile with repo notifications switched on."""
        rows = await self._run(Q_LIST_EVENT_ENABLED, ())
        return [_profile_from_row(row) for row in rows]

    async def list_capture_enabled(self) -> list[GroupProfile]:
        """Return every profile with message capture switched on."""
        rows = await self._run(Q_LIST_CAPTURE_ENABLED, ())
        return [_profile_from_row(row) for row in rows]

    async def get_cursors(self, scope: Scope) -> dict[str, str]:
        """Return the scope's per-resource poll cursor map."""
        rows = await self._run(Q_GET_CURSORS, _keys(scope))
        raw = rows[0][0] if rows else None
        if not raw:
            return {}
        loaded: dict[str, str] = json.loads(raw)
        return loaded

    async def set_cursors(self, scope: Scope, cursors: dict[str, str], repo: str) -> None:
        """Persist the per-resource cursor map iff still bound to ``repo``.

        The repo guard keeps an in-flight poll from stamping stale
        cursors onto a profile that was reset or rebound meanwhile.
        """
        payload = json.dumps(cursors, separators=(",", ":"))
        await self._run(Q_SET_CURSORS, (payload, *_keys(scope), repo))

    async def migrate(self, old: Scope, new: Scope) -> None:
        """Re-key every topic of a group after a group→supergroup upgrade.

        The collision-clear and the re-key run in one transaction: a crash
        between them must not delete the destination's rows while leaving
        the source's un-moved.
        """
        new_chat_id = int(new.channel)
        await self._run_tx(
            [
                (Q_MIGRATE_CLEAR, (new.platform, new.channel)),
                (Q_MIGRATE, (new.channel, new_chat_id, old.platform, old.channel)),
            ]
        )

    async def store_token(self, scope: Scope, token: str) -> None:
        """Encrypt and persist the scope's token."""
        ciphertext = self._fernet.encrypt(token.encode())
        platform, channel, thread = _keys(scope)
        chat_id, thread_id = _target(scope)
        await self._run(Q_VAULT_WRITE, (platform, channel, thread, chat_id, thread_id, ciphertext))

    async def fetch_token(self, scope: Scope) -> str | None:
        """Decrypt and return the scope's token, if one is stored.

        An undecryptable ciphertext (rotated key) reads as "no token"
        and is logged — the profile simply re-binds — rather than
        crashing every feature that consults the vault.
        """
        rows = await self._run(Q_VAULT_READ, _keys(scope))
        if not rows or rows[0][0] is None:
            return None
        try:
            return self._fernet.decrypt(bytes(rows[0][0])).decode()
        except InvalidToken:
            log_event("token_vault", "error")
            return None

    async def delete_token(self, scope: Scope) -> None:
        """Discard the scope's token."""
        await self._run(Q_VAULT_CLEAR, _keys(scope))

    async def get_actions(self, scope: Scope) -> tuple[ActionSpec, ...]:
        """Return the scope's actions, empty when none are configured."""
        rows = await self._run(Q_ACTIONS_READ, _keys(scope))
        return loads_actions(rows[0][0] if rows else None)

    async def set_actions(self, scope: Scope, actions: tuple[ActionSpec, ...]) -> None:
        """Replace the scope's actions (state included) wholesale."""
        platform, channel, thread = _keys(scope)
        chat_id, thread_id = _target(scope)
        await self._run(
            Q_ACTIONS_WRITE, (platform, channel, thread, chat_id, thread_id, dumps_actions(actions))
        )

    async def list_scheduled(self) -> list[tuple[Scope, tuple[ActionSpec, ...]]]:
        """Return every scope that has at least one action configured."""
        rows = await self._run(Q_ACTIONS_LIST, ())
        return [(Scope(row[0], row[1], row[2]), loads_actions(row[3])) for row in rows]

    async def _run(self, query: str, params: tuple[Any, ...]) -> list[tuple[Any, ...]]:
        try:
            return await asyncio.to_thread(self._runner.run, query, params)
        except (pymysql.MySQLError, OSError, KeyError) as error:
            log_event("storage", "error")
            msg = "profile store unavailable"
            raise StorageError(msg) from error

    async def _run_tx(self, statements: list[tuple[str, tuple[Any, ...]]]) -> None:
        try:
            await asyncio.to_thread(self._runner.run_tx, statements)
        except (pymysql.MySQLError, OSError, KeyError) as error:
            log_event("storage", "error")
            msg = "profile store unavailable"
            raise StorageError(msg) from error


def _profile_from_row(row: tuple[Any, ...]) -> GroupProfile:
    (
        platform,
        channel,
        thread,
        log_page,
        repo,
        consent,
        events_enabled,
        capture_enabled,
        rules_json,
        llm_json,
        subscribe_code,
        has_token,
    ) = row
    return GroupProfile(
        scope=Scope(platform, channel, thread),
        log_page=log_page,
        repo=repo,
        consent_mode=ConsentMode(consent) if consent else None,
        events_enabled=bool(events_enabled),
        capture_enabled=None if capture_enabled is None else bool(capture_enabled),
        rules=loads_rules(rules_json),
        llm=loads_llm(llm_json),
        subscribe_code=subscribe_code,
        has_token=bool(has_token),
    )
