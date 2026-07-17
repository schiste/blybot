"""Identifier-free operational logging and counters (spec section 16).

The privacy rule for logs — event types, outcomes, counts, and error
codes only; never message content or Telegram identifiers — is enforced
by keeping all operational logging behind :func:`log_event`.
"""

from __future__ import annotations

import logging
from collections import Counter
from typing import Final, Literal

logger: Final = logging.getLogger("blybot")

Outcome = Literal["ok", "declined", "error", "ignored", "retry"]
LogField = int | str


class Counters:
    """Process-lifetime operational counters (spec 16)."""

    def __init__(self) -> None:
        self._counts: Counter[str] = Counter()

    def increment(self, name: str, amount: int = 1) -> None:
        """Add ``amount`` to counter ``name``."""
        self._counts[name] += amount

    def snapshot(self) -> dict[str, int]:
        """Return a copy of all counters, for heartbeat logging."""
        return dict(self._counts)


def log_event(event: str, outcome: Outcome = "ok", **fields: LogField) -> None:
    """Log one operational event.

    ``event`` names what happened (``publish``, ``session_expired``,
    ...), ``outcome`` is drawn from a closed vocabulary, and extra
    ``fields`` are counts/durations or coarse machine error codes.
    """
    extras = "".join(f" {key}={value}" for key, value in sorted(fields.items()))
    logger.info("event=%s outcome=%s%s", event, outcome, extras)


def configure_logging(level: int = logging.INFO) -> None:
    """Send operational logs to stdout for the Toolforge jobs framework."""
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )
    # python-telegram-bot and httpx log request URLs at INFO; keep them
    # at WARNING so chat ids never appear in operational logs (spec 16).
    for noisy in ("httpx", "telegram"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
