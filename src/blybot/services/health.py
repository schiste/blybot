"""Startup and liveness observability, shared by every platform (issue #46).

Each adapter used to re-implement these three log lines, and they drifted:
Discord's heartbeat was added ad hoc in #28 with its own copy of the
archive-size try/except. They live here once so a new platform inherits
identical operational telemetry by calling the helper rather than by
copying a loop — and so the R6 rule (counts and outcomes only, never
content or identifiers) is enforced in one place.

How a platform drives them differs and that is fine: Telegram folds the
heartbeat into its existing maintenance tick, Discord runs
:func:`heartbeat_loop` as its own task. What must not differ is *what
gets logged*.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from blybot.domain.ports import StorageError
from blybot.observability import log_event

if TYPE_CHECKING:
    from blybot.domain.ports import MessageArchive
    from blybot.observability import Counters


def log_startup() -> None:
    """Mark the process ready. The line an operator greps for after a deploy."""
    log_event("startup", "ok")


def log_heartbeat(counters: Counters) -> None:
    """Prove liveness, carrying the counter snapshot.

    Without it a healthy instance and a crash-looping one look identical in
    the logs — the gap that motivated #28.
    """
    log_event("heartbeat", "ok", **counters.snapshot())


async def log_archive_size(archive: MessageArchive | None) -> None:
    """Report archive growth: row counts only, never content (R6).

    A storage outage is logged and swallowed — liveness reporting must not
    be the thing that takes the process down.
    """
    if archive is None:
        return
    try:
        log_event("archive_size", "ok", rows=await archive.total())
    except StorageError:
        log_event("archive_size", "error")


async def heartbeat_loop(
    counters: Counters, archive: MessageArchive | None, interval_seconds: float
) -> None:
    """Emit liveness (and archive size) forever on a fixed cadence.

    For platforms with no periodic tick of their own to hang this on; ones
    that already have a maintenance loop call the two helpers directly.
    """
    while True:
        await asyncio.sleep(interval_seconds)
        log_heartbeat(counters)
        await log_archive_size(archive)
