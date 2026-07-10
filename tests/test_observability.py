"""Observability tests (spec section 16): logs stay identifier-free."""

from __future__ import annotations

import inspect
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


def test_log_event_emits_event_outcome_and_numeric_fields(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.INFO, logger="blybot"):
        log_event("publish", "ok", attempts=2)
    assert caplog.messages == ["event=publish outcome=ok attempts=2"]


def test_log_event_fields_are_typed_int_only() -> None:
    """The signature is the privacy gate: no string field can carry content."""
    signature = inspect.signature(log_event)
    kwargs = signature.parameters["fields"]
    assert kwargs.kind is inspect.Parameter.VAR_KEYWORD
    assert kwargs.annotation in (int, "int")
