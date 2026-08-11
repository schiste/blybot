"""The subscription value object — the one domain type that carries a user id.

Every other domain value is deliberately identifier-free (spec R6); see
:mod:`blybot.domain.models`. A digest *subscription*, by its nature, must
remember *whom* to deliver to — the subscriber's DM :class:`Scope`, whose
``channel`` is the DM handle (on Telegram the private chat id, effectively
their user id). That identifier is isolated here, in its own module and
(durably) its own storage table, and never touches the pseudonymized
capture/content layer. This is the documented, opt-in carve-out to R6
(SPECIFICATION §21).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from datetime import datetime

    from blybot.domain.models import Schedule, Scope


@dataclass(frozen=True, slots=True)
class Subscription:
    """One user's standing request for a scope's recurring digest via DM.

    ``dm`` is the subscriber's DM :class:`Scope` — the sole durable
    Telegram identifier in the system, carried in its ``channel``.
    ``scope`` is the subscribed source group's structure, exactly as
    elsewhere. ``last_run`` is the per-subscription scheduler watermark
    (UTC).
    """

    sub_id: str
    dm: Scope
    scope: Scope
    schedule: Schedule
    recipe: str  # digest recipe: summarize | talking_points | stats
    lang: str
    last_run: datetime | None = None
    # An *inherited* subscription carries no schedule of its own: it means
    # "send me whatever this channel's owner configured", and is delivered
    # by that channel's action rather than by the subscription scheduler
    # (#71). `schedule`/`recipe`/`lang` are then placeholders the owner's
    # action overrides. Passing any option to `subscribe` makes it an
    # override instead, with its own independent run.
    inherited: bool = False

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
