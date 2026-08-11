"""Architecture fitness tests: the layering rules, enforced.

These keep the dependency arrows pointing inward (adapters -> services ->
domain) so the privacy boundary and testability survive future PRs. The
platform-specific rules are written against a discovered-and-generalized set
of platforms, so a second platform package (PR-5's Discord) is covered the
moment it lands — no test edits required.
"""

from __future__ import annotations

import ast
import importlib
import re
from pathlib import Path

from blybot.domain.models import PlatformCapabilities

SRC = Path(__file__).resolve().parent.parent / "src" / "blybot"
ADAPTERS = SRC / "adapters"

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

# Every chat platform and the import prefixes of its SDK. The core (domain +
# services) may import NONE of these; an adapter package may import only its
# OWN platform's SDK. Adding a platform here generalizes every rule below to it.
# A platform with no SDK maps to an empty tuple: IRC's line protocol is
# hand-rolled, so there is nothing to ban imports of — but it still has to
# be registered here, because this dict is also the roster of brand names
# the domain must never mention.
PLATFORM_SDKS: dict[str, tuple[str, ...]] = {
    "telegram": ("telegram",),
    "discord": ("discord", "discord.py"),
    "irc": (),
}

# The platform identity access that must never cross inward past its own
# adapter (the pseudonymization boundary, R6). Regexes, not substrings: the
# Telegram sentinel is a distinctive name, but Discord's identity object is
# reached through ``author``, which collides with the domain's OWN
# ``CapturedMessage.author`` — the already-pseudonymized label that is
# supposed to travel inward. Matching the *attribute chain* separates them:
# our author is a plain string and has no ``.id``/``.bot``/``.name``.
FORBIDDEN_IDENTITY_ATTRS: dict[str, str] = {
    "telegram": r"\bfrom_user\b",
    "discord": r"\bauthor\.(id|name|display_name|discriminator|bot|mention)\b",
}


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


def platform_packages() -> list[str]:
    """Discovered adapter platform packages: any subdir carrying a transport.py."""
    return sorted(
        child.name
        for child in ADAPTERS.iterdir()
        if child.is_dir() and (child / "transport.py").exists()
    )


def _all_platform_sdks() -> tuple[str, ...]:
    return tuple(prefix for sdks in PLATFORM_SDKS.values() for prefix in sdks)


def _code_strings_and_names(path: Path) -> set[str]:
    """Every string LITERAL and identifier in a module — excluding docstrings.

    A "platform-branded literal" is a string constant (e.g. ``"telegram"``) or
    an identifier (an imported name, a variable, an attribute) that names a
    platform in the *code*. Module/class/function docstrings are prose that
    deliberately describe the Telegram privacy boundary, so they are excluded —
    this guard is about branding leaking into the data model, not the
    documentation that explains why it must not.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    docstrings: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef):
            first = node.body[0] if node.body else None
            if (
                isinstance(first, ast.Expr)
                and isinstance(first.value, ast.Constant)
                and isinstance(first.value.value, str)
            ):
                docstrings.add(id(first.value))

    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            if id(node) not in docstrings:
                found.add(node.value)
        elif isinstance(node, ast.Name):
            found.add(node.id)
        elif isinstance(node, ast.Attribute):
            found.add(node.attr)
        elif isinstance(node, ast.arg):
            found.add(node.arg)
        elif isinstance(node, ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef):
            found.add(node.name)
        elif isinstance(node, ast.keyword) and node.arg:
            found.add(node.arg)
        elif isinstance(node, ast.alias):
            found.add(node.name)
            if node.asname:
                found.add(node.asname)
        elif isinstance(node, ast.ImportFrom) and node.module:
            found.add(node.module)
    return found


def _numeric_literals(path: Path) -> set[int]:
    """Every integer literal that appears in a module's code."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    return {
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and type(node.value) is int
    }


# --- Layering ----------------------------------------------------------------


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


# --- Platform-SDK isolation --------------------------------------------------


def test_core_imports_no_platform_sdk() -> None:
    """Domain and services import NONE of any platform's SDK (generalized)."""
    forbidden = _all_platform_sdks()
    for layer in ("domain", "services"):
        assert violations(SRC / layer, forbidden) == []


def test_every_discovered_platform_is_registered() -> None:
    """A new adapter package must declare its SDK prefixes (empty tuple if none).

    Without this, dropping in ``adapters/slack/`` would silently escape the
    brand-leak and foreign-SDK rules, which are both driven by
    :data:`PLATFORM_SDKS` rather than by what is on disk.
    """
    unregistered = sorted(set(platform_packages()) - set(PLATFORM_SDKS))
    assert unregistered == [], f"platform adapters missing from PLATFORM_SDKS: {unregistered}"


def test_each_platform_adapter_imports_only_its_own_sdk() -> None:
    """A platform package touches its own SDK only, never another platform's."""
    packages = platform_packages()
    assert packages, "no platform adapter packages discovered"
    for platform in packages:
        others = tuple(
            prefix for name, sdks in PLATFORM_SDKS.items() if name != platform for prefix in sdks
        )
        offenders = violations(ADAPTERS / platform, others)
        assert offenders == [], f"{platform} adapter imports a foreign platform SDK: {offenders}"


def test_every_platform_package_exposes_transport_and_capabilities() -> None:
    """Each platform package's transport module exports a Transport + capabilities."""
    for platform in platform_packages():
        module = importlib.import_module(f"blybot.adapters.{platform}.transport")

        caps = [
            (name, value) for name, value in vars(module).items() if name.endswith("_CAPABILITIES")
        ]
        assert caps, f"{platform}.transport exposes no *_CAPABILITIES constant"
        assert all(isinstance(value, PlatformCapabilities) for _, value in caps), (
            f"{platform}.transport's *_CAPABILITIES is not a PlatformCapabilities"
        )

        transports = [
            value
            for value in vars(module).values()
            if isinstance(value, type)
            and value.__module__ == module.__name__
            and hasattr(value, "send")
            and hasattr(value, "capabilities")
        ]
        assert transports, f"{platform}.transport exposes no Transport implementation"


# --- No platform branding in the data model / no size literals in services ---


def test_domain_holds_no_platform_branded_literal() -> None:
    """No platform name appears as a string literal or identifier in domain code.

    Sink/field names went neutral in PR-3; this locks that in. Prose
    docstrings that explain the Telegram boundary are intentionally exempt
    (see :func:`_code_strings_and_names`).
    """
    brands = tuple(PLATFORM_SDKS)  # ("telegram", "discord")
    offenders = []
    for path in (SRC / "domain").glob("*.py"):
        for token in _code_strings_and_names(path):
            lowered = token.casefold()
            if any(brand in lowered for brand in brands):
                offenders.append(f"{path.relative_to(SRC.parent)}: {token!r}")
    assert offenders == [], f"platform branding leaked into the domain data model: {offenders}"


def test_services_hold_no_hard_coded_message_size() -> None:
    """Per-message size caps live in PlatformCapabilities, never in services.

    PR-3 moved 4096 (Telegram) and 3500 (chunker headroom) into capabilities;
    a literal reappearing in services is drift back toward a hard-coded limit.
    """
    forbidden = {4096, 3500}
    offenders = []
    for path in (SRC / "services").glob("*.py"):
        leaked = forbidden & _numeric_literals(path)
        if leaked:
            offenders.append(f"{path.relative_to(SRC.parent)}: {sorted(leaked)}")
    assert offenders == [], f"hard-coded message-size literal in services: {offenders}"


# --- Identity boundary -------------------------------------------------------


def test_platform_identity_stays_at_its_own_adapter_boundary() -> None:
    """A platform's user-identity attribute never crosses inward past its adapter.

    ``from_user`` is the attribute every Telegram identity flows through, and
    ``author.id``/``.bot``/… every Discord one; keeping them out of domain,
    services, and the other adapters means captured content is pseudonymized
    before it crosses inward. The rule is keyed by platform, so a future
    platform's identity access is one dict entry.
    """
    for platform, pattern in FORBIDDEN_IDENTITY_ATTRS.items():
        allowed = ADAPTERS / platform
        probe = re.compile(pattern)
        for path in SRC.rglob("*.py"):
            if path.is_relative_to(allowed):
                continue
            found = probe.search(path.read_text(encoding="utf-8"))
            assert found is None, (
                f"{path.relative_to(SRC.parent)} reads {platform} user identity "
                f"({found.group(0) if found else pattern})"
            )


def test_the_relay_identity_never_reaches_storage_or_the_wiki() -> None:
    """``RelayMessage`` carries a real display name; nothing may persist it.

    The bridge (#76) is the one path where an unpseudonymized name travels,
    and it stays lawful under R6 only because it goes chat-to-chat and
    nowhere else: never to Meta, never to disk, never into the capture
    layer. That is a claim about *reachability*, so it is asserted here
    rather than left to the module docstring — the archive, the profile
    store and the wiki publisher must not so much as import the type.
    """
    forbidden = {
        SRC / "services" / "capture.py",
        SRC / "services" / "analyze.py",
        SRC / "services" / "publish.py",
        SRC / "services" / "subscriptions.py",
        *(SRC / "adapters" / "toolsdb").glob("*.py"),
        *(SRC / "adapters" / "mediawiki").glob("*.py"),
    }
    offenders = [
        path.relative_to(SRC.parent)
        for path in sorted(forbidden)
        if path.exists() and "domain.bridge" in path.read_text(encoding="utf-8")
    ]
    assert offenders == [], f"relay identity reached a persisting/publishing module: {offenders}"


def test_startup_and_liveness_logging_lives_in_one_place() -> None:
    """Issue #46: each adapter used to re-implement these lines and they drifted.

    Guards the extraction — a new platform must call `services.health`, not
    copy the loop. Keyed on the event names, so a fresh `log_event("heartbeat"…)`
    anywhere else fails regardless of how the surrounding code is shaped.
    """
    owner = SRC / "services" / "health.py"
    for event in ("startup", "heartbeat", "archive_size"):
        emitters = [
            path.relative_to(SRC.parent)
            for path in SRC.rglob("*.py")
            if path != owner and f'log_event("{event}"' in path.read_text(encoding="utf-8")
        ]
        assert not emitters, f"{event} is logged outside services/health.py: {emitters}"


def test_no_capability_is_declared_but_never_consulted() -> None:
    """A capability nobody gates on is worse than none: it looks authoritative.

    ``chat_picker`` sat in both descriptors, gated on by nothing, named after
    Telegram's picker *widget* rather than the platform fact underneath — and
    was duly misread as "Discord cannot do DM transcription" (#45). Scoped to
    the PlatformCapabilities body: a naive scan of models.py also collects
    GroupProfile/RuleFilter/CommandResult booleans this guard has no opinion
    about, so a failure would name an irrelevant field.
    """
    # Declared for a named future increment rather than forgotten. Anything
    # NOT listed here must have a live consumer.
    pending_by_design = {
        "threads": "informational; every platform served so far has them",
        "rich_choices": "the OutboundMessage.choices increment (#32) is unbuilt",
    }
    body = (
        (SRC / "domain" / "models.py")
        .read_text(encoding="utf-8")
        .split("class PlatformCapabilities:")[1]
        .split("\nclass ")[0]
    )
    fields = {
        line.split(":")[0].strip()
        for line in body.splitlines()
        if line.startswith("    ") and ": bool" in line
    }
    assert "durable_dm" in fields, "the scan did not find the capability class"
    consulted = "".join(
        path.read_text(encoding="utf-8")
        for path in SRC.rglob("*.py")
        if path != SRC / "domain" / "models.py"
    )
    unused = sorted(
        name for name in fields if f".{name}" not in consulted and name not in pending_by_design
    )
    assert not unused, f"capabilities declared but never consulted: {unused}"
    # And the acknowledgement list must not rot: a listed capability that HAS
    # gained a consumer should be removed from it.
    stale = sorted(name for name in pending_by_design if f".{name}" in consulted)
    assert not stale, f"listed as pending but now consulted: {stale}"
