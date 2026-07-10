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
from typing import TYPE_CHECKING, Any, Final, Protocol

import pymysql
from cryptography.fernet import Fernet, InvalidToken

from blybot.domain.models import ConsentMode, EventKind, GroupProfile
from blybot.domain.ports import StorageError
from blybot.observability import log_event

if TYPE_CHECKING:
    from pathlib import Path

SCHEMA: Final = """
CREATE TABLE IF NOT EXISTS profiles (
    chat_id BIGINT NOT NULL PRIMARY KEY,
    log_page VARCHAR(255) NULL,
    repo VARCHAR(140) NULL,
    consent_mode VARCHAR(16) NULL,
    events_enabled TINYINT(1) NOT NULL DEFAULT 0,
    event_kinds VARCHAR(64) NOT NULL DEFAULT '',
    token_ciphertext BLOB NULL,
    event_cursor VARCHAR(128) NULL,
    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP
)
"""

_PROFILE_COLUMNS: Final = (
    "chat_id, log_page, repo, consent_mode, events_enabled, event_kinds, "
    "token_ciphertext IS NOT NULL"
)
Q_GET: Final = f"SELECT {_PROFILE_COLUMNS} FROM profiles WHERE chat_id = %s"  # noqa: S608
Q_LIST_EVENT_ENABLED: Final = f"SELECT {_PROFILE_COLUMNS} FROM profiles WHERE events_enabled = 1"  # noqa: S608
Q_UPSERT: Final = """
INSERT INTO profiles (chat_id, log_page, repo, consent_mode, events_enabled, event_kinds)
VALUES (%s, %s, %s, %s, %s, %s)
ON DUPLICATE KEY UPDATE log_page = VALUES(log_page), repo = VALUES(repo),
    consent_mode = VALUES(consent_mode), events_enabled = VALUES(events_enabled),
    event_kinds = VALUES(event_kinds)
"""
Q_DELETE: Final = "DELETE FROM profiles WHERE chat_id = %s"
Q_GET_CURSOR: Final = "SELECT event_cursor FROM profiles WHERE chat_id = %s"
Q_SET_CURSOR: Final = "UPDATE profiles SET event_cursor = %s WHERE chat_id = %s"
Q_VAULT_READ: Final = "SELECT token_ciphertext FROM profiles WHERE chat_id = %s"
Q_VAULT_WRITE: Final = """
INSERT INTO profiles (chat_id, token_ciphertext) VALUES (%s, %s)
ON DUPLICATE KEY UPDATE token_ciphertext = VALUES(token_ciphertext)
"""
Q_VAULT_CLEAR: Final = "UPDATE profiles SET token_ciphertext = NULL WHERE chat_id = %s"


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
        """Create the schema if absent (additive DDL is the migration story)."""
        await self._run(SCHEMA, ())

    async def get(self, chat_id: int) -> GroupProfile | None:
        """Return the group's profile, or ``None`` if it never configured one."""
        rows = await self._run(Q_GET, (chat_id,))
        return _profile_from_row(rows[0]) if rows else None

    async def upsert(self, profile: GroupProfile) -> None:
        """Create or update the profile (token and cursor are untouched)."""
        await self._run(
            Q_UPSERT,
            (
                profile.chat_id,
                profile.log_page,
                profile.repo,
                profile.consent_mode.value if profile.consent_mode else None,
                int(profile.events_enabled),
                ",".join(sorted(kind.value for kind in profile.event_kinds)),
            ),
        )

    async def delete(self, chat_id: int) -> None:
        """Forget everything about the group, including its token and cursor."""
        await self._run(Q_DELETE, (chat_id,))

    async def list_event_enabled(self) -> list[GroupProfile]:
        """Return every profile with repo notifications switched on."""
        rows = await self._run(Q_LIST_EVENT_ENABLED, ())
        return [_profile_from_row(row) for row in rows]

    async def get_cursor(self, chat_id: int) -> str | None:
        """Return the group's event-poll cursor (ETag), if any."""
        rows = await self._run(Q_GET_CURSOR, (chat_id,))
        return rows[0][0] if rows and rows[0][0] else None

    async def set_cursor(self, chat_id: int, cursor: str) -> None:
        """Persist the group's event-poll cursor."""
        await self._run(Q_SET_CURSOR, (cursor, chat_id))

    async def store_token(self, chat_id: int, token: str) -> None:
        """Encrypt and persist the group's token."""
        ciphertext = self._fernet.encrypt(token.encode())
        await self._run(Q_VAULT_WRITE, (chat_id, ciphertext))

    async def fetch_token(self, chat_id: int) -> str | None:
        """Decrypt and return the group's token, if one is stored.

        An undecryptable ciphertext (rotated key) reads as "no token"
        and is logged — the group simply re-binds — rather than
        crashing every feature that consults the vault.
        """
        rows = await self._run(Q_VAULT_READ, (chat_id,))
        if not rows or rows[0][0] is None:
            return None
        try:
            return self._fernet.decrypt(bytes(rows[0][0])).decode()
        except InvalidToken:
            log_event("token_vault", "error")
            return None

    async def delete_token(self, chat_id: int) -> None:
        """Discard the group's token."""
        await self._run(Q_VAULT_CLEAR, (chat_id,))

    async def _run(self, query: str, params: tuple[Any, ...]) -> list[tuple[Any, ...]]:
        try:
            return await asyncio.to_thread(self._runner.run, query, params)
        except (pymysql.MySQLError, OSError, KeyError) as error:
            log_event("storage", "error")
            msg = "profile store unavailable"
            raise StorageError(msg) from error


def _profile_from_row(row: tuple[Any, ...]) -> GroupProfile:
    (chat_id, log_page, repo, consent, events_enabled, event_kinds, has_token) = row
    return GroupProfile(
        chat_id=int(chat_id),
        log_page=log_page,
        repo=repo,
        consent_mode=ConsentMode(consent) if consent else None,
        events_enabled=bool(events_enabled),
        event_kinds=frozenset(EventKind(kind) for kind in event_kinds.split(",") if kind),
        has_token=bool(has_token),
    )
