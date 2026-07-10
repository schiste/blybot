"""System-facing implementations of small infrastructure ports."""

from __future__ import annotations

from datetime import UTC, datetime


class SystemClock:
    """Wall-clock :class:`blybot.domain.ports.Clock` returning aware UTC datetimes."""

    def now(self) -> datetime:
        """Return the current UTC time."""
        return datetime.now(tz=UTC)
