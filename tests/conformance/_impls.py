"""The impl registries the contracts parametrize over — one entry per impl.

Every store port lists each of its implementations here as a ``(id, builder)``
pair. A contract fixture fans out over the list, so a future implementation
(e.g. PR-5's Discord-side store, or a Redis archive) proves itself against the
whole contract by adding exactly one line — and any impl whose behaviour drifts
from the port fails.

Both paths are real: the ``InMemory*`` fakes and the ``ToolsDb*`` adapters wired
to their SQL-level fake runner (from :mod:`tests._sql_fakes`), so the adapter's
own query/row logic is exercised, not a second copy of it.
"""

from __future__ import annotations

from collections.abc import Callable

from cryptography.fernet import Fernet

from blybot.adapters.toolsdb.archive import ToolsDbArchive
from blybot.adapters.toolsdb.store import ToolsDbStore
from blybot.adapters.toolsdb.subscriptions import ToolsDbSubscriptions
from blybot.domain.ports import (
    ActionStore,
    MessageArchive,
    ProfileStore,
    SubscriptionStore,
)
from tests._sql_fakes import FakeMessagesDb, FakeSubscriptionsDb, FakeToolsDb
from tests.fakes import (
    InMemoryActions,
    InMemoryArchive,
    InMemoryProfiles,
    InMemorySubscriptions,
)


def _inmemory_profiles() -> ProfileStore:
    return InMemoryProfiles()


def _toolsdb_store() -> ToolsDbStore:
    """A real ToolsDbStore over its SQL-level fake; serves ProfileStore + ActionStore."""
    return ToolsDbStore(runner=FakeToolsDb(), fernet_key=Fernet.generate_key().decode())


def _inmemory_archive() -> MessageArchive:
    return InMemoryArchive()


def _toolsdb_archive() -> MessageArchive:
    return ToolsDbArchive(runner=FakeMessagesDb())


def _inmemory_subscriptions() -> SubscriptionStore:
    return InMemorySubscriptions()


def _toolsdb_subscriptions() -> SubscriptionStore:
    return ToolsDbSubscriptions(runner=FakeSubscriptionsDb())


def _inmemory_actions() -> ActionStore:
    return InMemoryActions()


# One entry per implementation. Adding a future impl is a single line here.
PROFILE_STORES: list[tuple[str, Callable[[], ProfileStore]]] = [
    ("inmemory", _inmemory_profiles),
    ("toolsdb", _toolsdb_store),
]

MESSAGE_ARCHIVES: list[tuple[str, Callable[[], MessageArchive]]] = [
    ("inmemory", _inmemory_archive),
    ("toolsdb", _toolsdb_archive),
]

SUBSCRIPTION_STORES: list[tuple[str, Callable[[], SubscriptionStore]]] = [
    ("inmemory", _inmemory_subscriptions),
    ("toolsdb", _toolsdb_subscriptions),
]

ACTION_STORES: list[tuple[str, Callable[[], ActionStore]]] = [
    ("inmemory", _inmemory_actions),
    ("toolsdb", _toolsdb_store),
]
