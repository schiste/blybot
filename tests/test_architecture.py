"""Architecture fitness tests: the layering rules, enforced.

These keep the dependency arrows pointing inward (adapters -> services ->
domain) so the privacy boundary and testability survive future PRs.
"""

from __future__ import annotations

import ast
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent / "src" / "blybot"

THIRD_PARTY_IO = (
    "telegram",
    "mwclient",
    "pywikibot",
    "httpx",
    "requests",
    "aiohttp",
    "pymysql",
    "cryptography",
)


def imports_of(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            found.add(node.module)
    return found


def violations(layer: Path, forbidden_prefixes: tuple[str, ...]) -> list[str]:
    found = []
    for path in layer.rglob("*.py"):
        for module in imports_of(path):
            if module.startswith(forbidden_prefixes):
                found.append(f"{path.relative_to(SRC.parent)} imports {module}")
    return found


def test_domain_imports_neither_adapters_nor_services() -> None:
    forbidden = ("blybot.adapters", "blybot.services", "blybot.config")
    assert violations(SRC / "domain", forbidden) == []


def test_domain_and_services_import_no_io_libraries() -> None:
    for layer in ("domain", "services"):
        assert violations(SRC / layer, THIRD_PARTY_IO) == []


def test_services_do_not_import_adapters() -> None:
    assert violations(SRC / "services", ("blybot.adapters",)) == []


def test_nothing_persists_state_to_disk() -> None:
    """v1 has no persistent datastore (spec section 11, R6).

    Guards against accidental introduction of files/DBs holding session
    state or identifiers. The config loader reads the environment only.
    """
    forbidden = ("sqlite3", "shelve", "pickle", "dbm")
    for layer in ("domain", "services"):
        assert violations(SRC / layer, forbidden) == []


def test_user_identity_stays_at_the_telegram_boundary() -> None:
    """v3 capture: only the Telegram adapter may read message authors.

    ``from_user`` is the attribute every Telegram identity flows
    through; keeping it out of domain, services, and the other adapters
    means captured content is pseudonymized before it crosses inward.
    """
    allowed = SRC / "adapters" / "telegram"
    for path in SRC.rglob("*.py"):
        if path.is_relative_to(allowed):
            continue
        assert "from_user" not in path.read_text(
            encoding="utf-8"
        ), f"{path.relative_to(SRC.parent)} reads Telegram user identity"
