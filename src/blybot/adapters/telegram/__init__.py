"""Telegram adapter: update handlers and long-polling transport."""

from __future__ import annotations

import os

# Opt into python-telegram-bot's forthcoming timedelta semantics for
# time-period attributes (e.g. RetryAfter.retry_after). PTB reads this at
# call time, so setting it before the adapter runs is enough; it removes
# the v22.2 deprecation warning and future-proofs us for the next major.
# setdefault so an operator's explicit choice still wins.
os.environ.setdefault("PTB_TIMEDELTA", "1")
