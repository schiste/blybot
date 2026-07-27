"""The subscription value object — the one domain type that carries a user id.

Every other domain value is deliberately identifier-free (spec R6); see
:mod:`blybot.domain.models`. A digest *subscription*, by its nature, must
remember *whom* to deliver to — the subscriber's private (DM) chat id,
which for a private chat is effectively their Telegram user id. That
identifier is isolated here, in its own module and (durably) its own
storage table, and never touches the pseudonymized capture/content layer.
This is the documented, opt-in carve-out to R6 (SPECIFICATION §21).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from datetime import datetime

    from blybot.domain.models import Schedule


@dataclass(frozen=True, slots=True)
class Subscription:
    """One user's standing request for a scope's recurring digest via DM.

    ``dm_chat_id`` is the subscriber's private chat — the sole durable
    Telegram identifier in the system. ``chat_id``/``thread_id`` are the
    subscribed scope's *group structure*, exactly as elsewhere.
    ``last_run`` is the per-subscription scheduler watermark (UTC).
    """

    sub_id: str
    dm_chat_id: int
    chat_id: int
    thread_id: int
    schedule: Schedule
    recipe: str  # digest recipe: summarize | talking_points | stats
    lang: str
    last_run: datetime | None = None

    def __post_init__(self) -> None:
        if not self.sub_id:
            msg = "Subscription sub_id must be non-empty"
            raise ValueError(msg)
        if not self.recipe:
            msg = "Subscription recipe must be non-empty"
            raise ValueError(msg)
        if not self.lang:
            msg = "Subscription lang must be non-empty"
            raise ValueError(msg)
