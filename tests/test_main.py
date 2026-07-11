"""Composition-root tests: python -m blybot wiring."""

from __future__ import annotations

from typing import Any

import pytest
from cryptography.fernet import Fernet

import blybot.__main__ as entry
from blybot.adapters.mediawiki.publisher import MetaWikiPublisher
from blybot.adapters.telegram.admin import AdminHandlers
from blybot.adapters.telegram.app import Lifecycle
from blybot.adapters.telegram.handlers import GroupHandlers, PrivateHandlers
from blybot.adapters.toolsdb.store import ToolsDbStore
from tests.test_config import REQUIRED


def test_missing_configuration_exits_2_without_echoing_values(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    for key in REQUIRED:
        monkeypatch.delenv(key, raising=False)

    assert entry.main() == 2
    err = capsys.readouterr().err
    assert "configuration error" in err
    assert "TELEGRAM_BOT_TOKEN" in err


async def test_main_wires_the_full_object_graph(monkeypatch: pytest.MonkeyPatch) -> None:
    for key, value in REQUIRED.items():
        monkeypatch.setenv(key, value)
    seen: dict[str, Any] = {}

    def fake_run_polling(**kwargs: Any) -> None:
        seen.update(kwargs)

    monkeypatch.setattr(entry, "run_polling", fake_run_polling)

    assert entry.main() == 0
    assert seen["token"] == REQUIRED["TELEGRAM_BOT_TOKEN"]
    assert isinstance(seen["group_handlers"], GroupHandlers)
    assert isinstance(seen["private_handlers"], PrivateHandlers)
    lifecycle = seen["lifecycle"]
    assert isinstance(lifecycle, Lifecycle)
    # One shared counters instance and one shared session registry.
    assert lifecycle.maintenance.counters is seen["group_handlers"].counters
    assert lifecycle.maintenance.sessions is seen["private_handlers"].sessions
    # Shutdown releases the HTTP clients via the composed closure.
    assert isinstance(lifecycle.transcription.publisher, MetaWikiPublisher)
    assert lifecycle.release.__name__ == "release_clients"
    # Group /log and DM transcription target the configured pages.
    assert isinstance(seen["admin_handlers"], AdminHandlers)
    directory = seen["group_handlers"].directory
    assert seen["admin_handlers"].directory is directory  # one directory, shared
    assert directory.default_log_page == REQUIRED["LOG_TARGET_PAGE"]
    assert directory.default_repo == ""  # never the operator's own /bug repo
    assert lifecycle.transcription.target_page == REQUIRED["DM_TARGET_BASE"]
    await lifecycle.release()  # v1 mode: no /bug tracker client to close


async def test_valid_encryption_key_enables_self_service(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for key, value in REQUIRED.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("PROFILE_ENCRYPTION_KEY", Fernet.generate_key().decode())
    monkeypatch.setenv("WIKI_PAGE_SUFFIX", "Telegram logs")
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_dummy")  # builds the /bug tracker too
    seen: dict[str, Any] = {}
    monkeypatch.setattr(entry, "run_polling", lambda **kwargs: seen.update(kwargs))

    assert entry.main() == 0
    directory = seen["group_handlers"].directory
    assert isinstance(directory.store, ToolsDbStore)
    assert directory.page_suffix == "Telegram logs"
    assert seen["lifecycle"].bootstrap is not None
    await seen["lifecycle"].release()  # closes both HTTP clients cleanly


def test_invalid_encryption_key_fails_fast(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    for key, value in REQUIRED.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("PROFILE_ENCRYPTION_KEY", "not-a-fernet-key")

    assert entry.main() == 2
    assert "Fernet" in capsys.readouterr().err
