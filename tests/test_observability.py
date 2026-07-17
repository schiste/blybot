"""Observability tests (spec section 16): logs stay identifier-free."""

from __future__ import annotations

import logging

import pytest

from blybot.observability import Counters, log_event


def test_counters_accumulate_and_snapshot() -> None:
    counters = Counters()
    counters.increment("publish_succeeded")
    counters.increment("publish_succeeded")
    counters.increment("api_retries", 3)
    assert counters.snapshot() == {"publish_succeeded": 2, "api_retries": 3}


def test_snapshot_is_a_copy() -> None:
    counters = Counters()
    counters.increment("x")
    snap = counters.snapshot()
    snap["x"] = 99
    assert counters.snapshot() == {"x": 1}


def test_log_event_emits_event_outcome_and_fields(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.INFO, logger="blybot"):
        log_event("publish", "ok", attempts=2, code="maxlag")
    assert caplog.messages == ["event=publish outcome=ok attempts=2 code=maxlag"]


def test_log_event_keeps_machine_codes_identifier_free(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.INFO, logger="blybot"):
        log_event("wiki_edit", "error", code="protectedpage")
    assert caplog.messages == ["event=wiki_edit outcome=error code=protectedpage"]
