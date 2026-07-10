"""SystemClock returns aware UTC datetimes (naive datetimes break TTL math)."""

from __future__ import annotations

from datetime import UTC

from blybot.adapters.system import SystemClock


def test_now_is_timezone_aware_utc() -> None:
    now = SystemClock().now()
    assert now.tzinfo is UTC
