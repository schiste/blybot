"""Observability tests (spec section 16): logs stay identifier-free."""

from __future__ import annotations

import logging

import pytest

from blybot.observability import Counters, _one_line, log_event


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


def test_a_field_value_cannot_forge_a_second_log_line(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Issue #26: LogField allows str, and some values are upstream-controlled
    (a MediaWiki error code, say). One newline would render a single event as
    two lines, the second attacker-shaped."""
    with caplog.at_level(logging.INFO, logger="blybot"):
        log_event("wiki_edit", "error", code="badtoken\nevent=publish outcome=ok")

    (message,) = caplog.messages
    assert "\n" not in message  # still exactly one line
    assert "code=badtoken?event=publish" in message  # tampering left visible
    # The forged event never appears as its own record.
    assert len(caplog.records) == 1


def test_control_characters_are_replaced_not_dropped() -> None:
    """Replacing keeps the tampering auditable; dropping would hide it."""
    assert _one_line("a\rb\x00c\x7fd") == "a?b?c?d"
    assert _one_line(42) == 42  # counts pass through untouched
