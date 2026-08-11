"""Composition-root tests: python -m blybot wiring."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from cryptography.fernet import Fernet

import blybot.__main__ as entry
from blybot.adapters.mediawiki.publisher import MetaWikiPublisher
from blybot.adapters.telegram.admin import AdminHandlers
from blybot.adapters.telegram.app import Lifecycle
from blybot.adapters.telegram.handlers import GroupHandlers, PrivateHandlers
from blybot.adapters.toolsdb.archive import ToolsDbArchive
from blybot.adapters.toolsdb.store import ToolsDbStore
from blybot.adapters.toolsdb.subscriptions import ToolsDbSubscriptions
from blybot.config import load_config
from blybot.domain.models import Scope
from blybot.domain.ports import ActionError
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


def _capturing(seen: dict[str, Any]) -> Any:
    """Stand in for `build_polling_application`, recording its wiring.

    Returns the (application, allowed) pair the root now unpacks; the
    application only has to be something `run_alone` can be built from,
    and these tests never run it.
    """

    def build(**kwargs: Any) -> tuple[Any, list[str]]:
        seen.update(kwargs)
        return SimpleNamespace(bot=object(), run_polling=lambda **_: None), []

    return build


async def test_main_wires_the_full_object_graph(monkeypatch: pytest.MonkeyPatch) -> None:
    for key, value in REQUIRED.items():
        monkeypatch.setenv(key, value)
    # v1 mode must not depend on the ambient environment (CI runners and
    # dev containers commonly export GITHUB_TOKEN).
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    seen: dict[str, Any] = {}

    fake_run_polling = _capturing(seen)

    monkeypatch.setattr(entry, "build_polling_application", fake_run_polling)

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
    monkeypatch.setattr(entry, "build_polling_application", _capturing(seen))

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


async def test_pseudonym_key_enables_capture_wiring(monkeypatch: pytest.MonkeyPatch) -> None:
    for key, value in REQUIRED.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("PROFILE_ENCRYPTION_KEY", Fernet.generate_key().decode())
    monkeypatch.setenv("ARCHIVE_PSEUDONYM_KEY", "long-random-operator-key")
    seen: dict[str, Any] = {}
    monkeypatch.setattr(entry, "build_polling_application", _capturing(seen))

    assert entry.main() == 0
    assert seen["capture_handlers"] is not None
    admin = seen["admin_handlers"]
    assert admin.archive is not None
    assert admin.capture_service is seen["capture_handlers"].service
    # Supergroup migrations re-key the captured messages too.
    assert seen["group_handlers"].archive is admin.archive
    # Pending consent revocations converge on the maintenance tick.
    lifecycle = seen["lifecycle"]
    assert lifecycle.maintenance.capture is seen["capture_handlers"].service
    await lifecycle.release()


async def test_capture_stays_off_without_the_pseudonym_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for key, value in REQUIRED.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("PROFILE_ENCRYPTION_KEY", Fernet.generate_key().decode())
    monkeypatch.delenv("ARCHIVE_PSEUDONYM_KEY", raising=False)
    seen: dict[str, Any] = {}
    monkeypatch.setattr(entry, "build_polling_application", _capturing(seen))

    assert entry.main() == 0
    assert seen["capture_handlers"] is None
    assert seen["admin_handlers"].archive is None

    async def store_boot(_self: object) -> None:
        return None

    monkeypatch.setattr(ToolsDbStore, "bootstrap", store_boot)
    await seen["lifecycle"].bootstrap()  # no archive: profile store only
    await seen["lifecycle"].release()


async def test_bootstrap_covers_both_stores(monkeypatch: pytest.MonkeyPatch) -> None:
    for key, value in REQUIRED.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("PROFILE_ENCRYPTION_KEY", Fernet.generate_key().decode())
    monkeypatch.setenv("ARCHIVE_PSEUDONYM_KEY", "long-random-operator-key")
    seen: dict[str, Any] = {}
    monkeypatch.setattr(entry, "build_polling_application", _capturing(seen))
    booted: list[str] = []

    async def store_boot(_self: object) -> None:
        booted.append("profiles")

    async def archive_boot(_self: object) -> None:
        booted.append("messages")

    async def subs_boot(_self: object) -> None:
        booted.append("subscriptions")

    monkeypatch.setattr(ToolsDbStore, "bootstrap", store_boot)
    monkeypatch.setattr(ToolsDbArchive, "bootstrap", archive_boot)
    monkeypatch.setattr(ToolsDbSubscriptions, "bootstrap", subs_boot)

    assert entry.main() == 0
    bootstrap = seen["lifecycle"].bootstrap
    assert bootstrap is not None
    await bootstrap()
    assert booted == ["profiles", "messages", "subscriptions"]
    await seen["lifecycle"].release()


async def test_capture_wiring_builds_the_analysis_pipeline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for key, value in REQUIRED.items():
        monkeypatch.setenv(key, value)
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)  # no issue_tracker sink
    monkeypatch.setenv("PROFILE_ENCRYPTION_KEY", Fernet.generate_key().decode())
    monkeypatch.setenv("ARCHIVE_PSEUDONYM_KEY", "long-random-operator-key")
    seen: dict[str, Any] = {}
    monkeypatch.setattr(entry, "build_polling_application", _capturing(seen))

    assert entry.main() == 0
    handlers = seen["analysis_handlers"]
    assert handlers is not None
    engine = handlers.analysis.engine
    # One engine serves every pipeline this deployment enables.
    assert set(engine.sources) == {"archive_window", "repo_events"}
    assert set(engine.transforms) == {"prompt", "stats", "log_publish", "rule_match"}
    assert set(engine.sinks) == {
        "wiki_section",
        "reply",
        "chat_confirm",
        "chat_message",
    }
    # The wiki sink refuses to publish for a scope that never ran
    # /setpage — same policy as /log on self-service deployments.
    sink = engine.sinks["wiki_section"]
    with pytest.raises(ActionError, match="/setpage"):
        await sink.resolve_page(Scope("telegram", "-1"), None)
    assert seen["admin_handlers"].commands.llm_defaults is not None
    await seen["lifecycle"].release()  # also closes the LiftWing client


async def test_capture_wiring_schedules_actions_on_the_tick(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for key, value in REQUIRED.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("PROFILE_ENCRYPTION_KEY", Fernet.generate_key().decode())
    monkeypatch.setenv("ARCHIVE_PSEUDONYM_KEY", "long-random-operator-key")
    seen: dict[str, Any] = {}
    monkeypatch.setattr(entry, "build_polling_application", _capturing(seen))

    assert entry.main() == 0
    lifecycle = seen["lifecycle"]
    assert lifecycle.scheduler is not None
    assert lifecycle.scheduler.engine is seen["analysis_handlers"].analysis.engine
    # /action is configured through the shared CommandService now.
    assert seen["admin_handlers"].commands.actions is not None
    assert seen["admin_handlers"].commands.clock is not None
    await lifecycle.release()


async def test_reannounce_cadence_wires_the_reminder(monkeypatch: pytest.MonkeyPatch) -> None:
    for key, value in REQUIRED.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("PROFILE_ENCRYPTION_KEY", Fernet.generate_key().decode())
    monkeypatch.setenv("ARCHIVE_PSEUDONYM_KEY", "long-random-operator-key")
    seen: dict[str, Any] = {}
    monkeypatch.setattr(entry, "build_polling_application", _capturing(seen))

    assert entry.main() == 0
    assert seen["lifecycle"].reminder is None  # default: reminders off
    assert seen["lifecycle"].maintenance.archive is not None  # size metric on

    monkeypatch.setenv("CAPTURE_REANNOUNCE_DAYS", "30")
    assert entry.main() == 0
    rewired = seen["lifecycle"]
    assert rewired.reminder is not None
    assert rewired.reminder.cadence.days == 30
    await rewired.release()


def test_the_unified_start_polls_on_the_callers_loop(monkeypatch: pytest.MonkeyPatch) -> None:
    """Two platforms in one process cannot both own the loop, so the
    runtime's `start` is the async poller — not `run_polling` (#78)."""
    for key, value in REQUIRED.items():
        monkeypatch.setenv(key, value)
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    polled: list[tuple[Any, list[str]]] = []

    def fake_poll(application: Any, allowed: list[str]) -> Any:
        polled.append((application, allowed))
        return _closed()

    monkeypatch.setattr(entry, "build_polling_application", _capturing({}))
    monkeypatch.setattr(entry, "poll_until_cancelled", fake_poll)

    runtime = entry.build_telegram(load_config())
    runtime.start().close()

    assert len(polled) == 1
    assert runtime.platform == "telegram"
    assert runtime.run_alone is not None  # ...but the isolated job still has its own


async def _closed() -> None:  # pragma: no cover -- closed unstarted
    return None
