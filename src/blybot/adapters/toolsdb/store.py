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

from blybot.domain.models import ConsentMode, GroupProfile
from blybot.domain.ports import StorageError
from blybot.observability import log_event
from blybot.services.rules import dumps_rules, loads_rules

if TYPE_CHECKING:
    from pathlib import Path

SCHEMA: Final = """
CREATE TABLE IF NOT EXISTS profiles (
    chat_id BIGINT NOT NULL,
    thread_id BIGINT NOT NULL DEFAULT 0,
    log_page VARCHAR(255) NULL,
    repo VARCHAR(140) NULL,
    consent_mode VARCHAR(16) NULL,
    events_enabled TINYINT(1) NOT NULL DEFAULT 0,
    rules_json TEXT NULL,
    cursors_json TEXT NULL,
    token_ciphertext BLOB NULL,
    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    PRIMARY KEY (chat_id, thread_id)
)
"""

_PROFILE_COLUMNS: Final = (
    "chat_id, thread_id, log_page, repo, consent_mode, events_enabled, "
    "rules_json, token_ciphertext IS NOT NULL"
)
_KEY: Final = "chat_id = %s AND thread_id = %s"
Q_GET: Final = f"SELECT {_PROFILE_COLUMNS} FROM profiles WHERE {_KEY}"  # noqa: S608
Q_LIST_EVENT_ENABLED: Final = f"SELECT {_PROFILE_COLUMNS} FROM profiles WHERE events_enabled = 1"  # noqa: S608
Q_UPSERT: Final = """
INSERT INTO profiles
    (chat_id, thread_id, log_page, repo, consent_mode, events_enabled, rules_json)
VALUES (%s, %s, %s, %s, %s, %s, %s)
ON DUPLICATE KEY UPDATE log_page = VALUES(log_page), repo = VALUES(repo),
    consent_mode = VALUES(consent_mode), events_enabled = VALUES(events_enabled),
    rules_json = VALUES(rules_json)
"""
Q_DELETE: Final = f"DELETE FROM profiles WHERE {_KEY}"  # noqa: S608
Q_GET_CURSORS: Final = f"SELECT cursors_json FROM profiles WHERE {_KEY}"  # noqa: S608
Q_SET_CURSORS: Final = f"UPDATE profiles SET cursors_json = %s WHERE {_KEY} AND repo = %s"  # noqa: S608
# Group→supergroup migration re-keys the group's rows to the new chat
# id. The migrating group is authoritative, so any pre-existing rows at
# the destination (a chat id the bot somehow already knew) are cleared
# first — otherwise the UPDATE would collide on the (chat_id, thread_id)
# primary key and silently strand rows.
Q_MIGRATE_CLEAR: Final = "DELETE FROM profiles WHERE chat_id = %s"
Q_MIGRATE: Final = "UPDATE profiles SET chat_id = %s WHERE chat_id = %s"

# Idempotent in-place schema upgrade for tables created before the
# thread_id column existed. Runs on every startup; each step no-ops once
# applied, so no data is ever dropped.
MIGRATE_ADD_THREAD: Final = (
    "ALTER TABLE profiles ADD COLUMN IF NOT EXISTS thread_id BIGINT NOT NULL DEFAULT 0"
)
MIGRATE_REBUILD_PK: Final = (
    "ALTER TABLE profiles DROP PRIMARY KEY, ADD PRIMARY KEY (chat_id, thread_id)"
)
# Composable rules and per-resource poll cursors arrived after the
# original schema; older tables gain the columns in place. No-op once
# applied. (The retired event_kinds/event_cursor columns are simply
# left in place on old tables — unused and harmless.)
MIGRATE_ADD_RULES: Final = "ALTER TABLE profiles ADD COLUMN IF NOT EXISTS rules_json TEXT NULL"
MIGRATE_ADD_CURSORS: Final = "ALTER TABLE profiles ADD COLUMN IF NOT EXISTS cursors_json TEXT NULL"
Q_THREAD_IN_PK: Final = """
SELECT COUNT(*) FROM information_schema.STATISTICS
WHERE table_schema = DATABASE() AND table_name = 'profiles'
  AND index_name = 'PRIMARY' AND column_name = 'thread_id'
"""
Q_VAULT_READ: Final = f"SELECT token_ciphertext FROM profiles WHERE {_KEY}"  # noqa: S608
Q_VAULT_WRITE: Final = """
INSERT INTO profiles (chat_id, thread_id, token_ciphertext) VALUES (%s, %s, %s)
ON DUPLICATE KEY UPDATE token_ciphertext = VALUES(token_ciphertext)
"""
Q_VAULT_CLEAR: Final = f"UPDATE profiles SET token_ciphertext = NULL WHERE {_KEY}"  # noqa: S608


class SqlRunner(Protocol):
    """Executes one SQL statement synchronously; returns all rows."""

    def run(self, query: str, params: tuple[Any, ...]) -> list[tuple[Any, ...]]:
        """Run ``query`` with ``params``; empty list for writes."""
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

    def _credentials(self) -> tuple[str, str]:
        parser = configparser.ConfigParser()
        parser.read(self._cnf_path)
        client = parser["client"]
        return client["user"].strip("'\""), client["password"].strip("'\"")


class ToolsDbStore:
    """ProfileStore + TokenVault backed by one ToolsDB table."""

    def __init__(self, runner: SqlRunner, fernet_key: str) -> None:
        self._runner = runner
        self._fernet = Fernet(fernet_key)

    async def bootstrap(self) -> None:
        """Create the schema, then bring an older table up to date in place.

        Idempotent: a fresh table is created complete by ``SCHEMA``; an
        older single-column-keyed table gains ``thread_id`` and has its
        primary key rebuilt to ``(chat_id, thread_id)`` — without
        dropping any rows. Every step is a no-op once applied.
        """
        await self._run(SCHEMA, ())
        await self._run(MIGRATE_ADD_THREAD, ())
        await self._run(MIGRATE_ADD_RULES, ())
        await self._run(MIGRATE_ADD_CURSORS, ())
        rows = await self._run(Q_THREAD_IN_PK, ())
        if rows and not int(rows[0][0]):
            await self._run(MIGRATE_REBUILD_PK, ())
            log_event("storage_migrated", "ok")

    async def get(self, chat_id: int, thread_id: int) -> GroupProfile | None:
        """Return the (group, topic) profile, or ``None`` if unconfigured."""
        rows = await self._run(Q_GET, (chat_id, thread_id))
        return _profile_from_row(rows[0]) if rows else None

    async def upsert(self, profile: GroupProfile) -> None:
        """Create or update the profile (token and cursors are untouched)."""
        await self._run(
            Q_UPSERT,
            (
                profile.chat_id,
                profile.thread_id,
                profile.log_page,
                profile.repo,
                profile.consent_mode.value if profile.consent_mode else None,
                int(profile.events_enabled),
                dumps_rules(profile.rules),
            ),
        )

    async def delete(self, chat_id: int, thread_id: int) -> None:
        """Forget everything about the (group, topic), token and cursor included."""
        await self._run(Q_DELETE, (chat_id, thread_id))

    async def list_event_enabled(self) -> list[GroupProfile]:
        """Return every profile with repo notifications switched on."""
        rows = await self._run(Q_LIST_EVENT_ENABLED, ())
        return [_profile_from_row(row) for row in rows]

    async def get_cursors(self, chat_id: int, thread_id: int) -> dict[str, str]:
        """Return the (group, topic) per-resource poll cursor map."""
        rows = await self._run(Q_GET_CURSORS, (chat_id, thread_id))
        raw = rows[0][0] if rows else None
        if not raw:
            return {}
        loaded: dict[str, str] = json.loads(raw)
        return loaded

    async def set_cursors(
        self, chat_id: int, thread_id: int, cursors: dict[str, str], repo: str
    ) -> None:
        """Persist the per-resource cursor map iff still bound to ``repo``.

        The repo guard keeps an in-flight poll from stamping stale
        cursors onto a profile that was reset or rebound meanwhile.
        """
        payload = json.dumps(cursors, separators=(",", ":"))
        await self._run(Q_SET_CURSORS, (payload, chat_id, thread_id, repo))

    async def migrate(self, old_chat_id: int, new_chat_id: int) -> None:
        """Re-key every topic of a group after a group→supergroup upgrade."""
        await self._run(Q_MIGRATE_CLEAR, (new_chat_id,))
        await self._run(Q_MIGRATE, (new_chat_id, old_chat_id))

    async def store_token(self, chat_id: int, thread_id: int, token: str) -> None:
        """Encrypt and persist the (group, topic) token."""
        ciphertext = self._fernet.encrypt(token.encode())
        await self._run(Q_VAULT_WRITE, (chat_id, thread_id, ciphertext))

    async def fetch_token(self, chat_id: int, thread_id: int) -> str | None:
        """Decrypt and return the (group, topic) token, if one is stored.

        An undecryptable ciphertext (rotated key) reads as "no token"
        and is logged — the profile simply re-binds — rather than
        crashing every feature that consults the vault.
        """
        rows = await self._run(Q_VAULT_READ, (chat_id, thread_id))
        if not rows or rows[0][0] is None:
            return None
        try:
            return self._fernet.decrypt(bytes(rows[0][0])).decode()
        except InvalidToken:
            log_event("token_vault", "error")
            return None

    async def delete_token(self, chat_id: int, thread_id: int) -> None:
        """Discard the (group, topic) token."""
        await self._run(Q_VAULT_CLEAR, (chat_id, thread_id))

    async def _run(self, query: str, params: tuple[Any, ...]) -> list[tuple[Any, ...]]:
        try:
            return await asyncio.to_thread(self._runner.run, query, params)
        except (pymysql.MySQLError, OSError, KeyError) as error:
            log_event("storage", "error")
            msg = "profile store unavailable"
            raise StorageError(msg) from error


def _profile_from_row(row: tuple[Any, ...]) -> GroupProfile:
    (
        chat_id,
        thread_id,
        log_page,
        repo,
        consent,
        events_enabled,
        rules_json,
        has_token,
    ) = row
    return GroupProfile(
        chat_id=int(chat_id),
        thread_id=int(thread_id),
        log_page=log_page,
        repo=repo,
        consent_mode=ConsentMode(consent) if consent else None,
        events_enabled=bool(events_enabled),
        rules=loads_rules(rules_json),
        has_token=bool(has_token),
    )
