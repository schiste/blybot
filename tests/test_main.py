"""Composition-root tests: python -m blybot wiring."""

from __future__ import annotations

from typing import Any

import pytest

import blybot.__main__ as entry
from blybot.adapters.mediawiki.publisher import MetaWikiPublisher
from blybot.adapters.telegram.admin import AdminHandlers
from blybot.adapters.telegram.app import Lifecycle
from blybot.adapters.telegram.handlers import GroupHandlers, PrivateHandlers
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


def test_main_wires_the_full_object_graph(monkeypatch: pytest.MonkeyPatch) -> None:
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
    # Shutdown releases the wiki client the services publish through.
    assert isinstance(lifecycle.transcription.publisher, MetaWikiPublisher)
    assert lifecycle.release == lifecycle.transcription.publisher.aclose
    # Group /log and DM transcription target the configured pages.
    assert isinstance(seen["admin_handlers"], AdminHandlers)
    directory = seen["group_handlers"].directory
    assert seen["admin_handlers"].directory is directory  # one directory, shared
    assert directory.default_log_page == REQUIRED["LOG_TARGET_PAGE"]
    assert lifecycle.transcription.target_page == REQUIRED["DM_TARGET_BASE"]
