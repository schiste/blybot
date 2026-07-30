"""Shared startup/liveness observability (issue #46).

These assertions used to live in the Discord composition tests, against a
Discord-specific heartbeat. The behavior is neutral now, so the tests are
too — one place proving what every platform logs.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, cast

import pytest

from blybot.domain.ports import StorageError
from blybot.observability import Counters
from blybot.services.health import (
    heartbeat_loop,
    log_archive_size,
    log_heartbeat,
    log_startup,
)


class _StubArchive:
    def __init__(self, *, total: int | None = None) -> None:
        self._total = total

    async def total(self) -> int:
        if self._total is None:
            raise StorageError
        return self._total


def _sleep_then_cancel(monkeypatch: pytest.MonkeyPatch) -> None:
    """Let the loop run exactly one body, then break its endless loop."""
    calls = {"n": 0}

    async def fake_sleep(_seconds: float) -> None:
        calls["n"] += 1
        if calls["n"] >= 2:
            raise asyncio.CancelledError

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)


def test_startup_marks_the_process_ready(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.INFO, logger="blybot"):
        log_startup()
    assert any("event=startup outcome=ok" in m for m in caplog.messages)


def test_heartbeat_carries_the_counter_snapshot(caplog: pytest.LogCaptureFixture) -> None:
    counters = Counters()
    counters.increment("publish_succeeded", 3)
    with caplog.at_level(logging.INFO, logger="blybot"):
        log_heartbeat(counters)
    (message,) = caplog.messages
    assert "event=heartbeat outcome=ok" in message
    assert "publish_succeeded=3" in message


async def test_archive_size_reports_rows_never_content(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.INFO, logger="blybot"):
        await log_archive_size(cast("Any", _StubArchive(total=7)))
    assert any("event=archive_size outcome=ok rows=7" in m for m in caplog.messages)


async def test_archive_size_swallows_an_outage(caplog: pytest.LogCaptureFixture) -> None:
    """Liveness reporting must never be the thing that takes the process down."""
    with caplog.at_level(logging.INFO, logger="blybot"):
        await log_archive_size(cast("Any", _StubArchive(total=None)))
    assert any("event=archive_size outcome=error" in m for m in caplog.messages)


async def test_archive_size_is_silent_without_an_archive(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.INFO, logger="blybot"):
        await log_archive_size(None)
    assert caplog.messages == []


async def test_the_loop_beats_and_reports_the_archive(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    _sleep_then_cancel(monkeypatch)
    with caplog.at_level(logging.INFO, logger="blybot"), pytest.raises(asyncio.CancelledError):
        await heartbeat_loop(Counters(), cast("Any", _StubArchive(total=7)), 900.0)
    assert any("event=heartbeat outcome=ok" in m for m in caplog.messages)
    assert any("event=archive_size outcome=ok rows=7" in m for m in caplog.messages)


async def test_the_loop_beats_without_an_archive(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    _sleep_then_cancel(monkeypatch)
    with caplog.at_level(logging.INFO, logger="blybot"), pytest.raises(asyncio.CancelledError):
        await heartbeat_loop(Counters(), None, 900.0)
    assert any("event=heartbeat outcome=ok" in m for m in caplog.messages)
    assert not any("archive_size" in m for m in caplog.messages)
