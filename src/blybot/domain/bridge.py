"""The relay value object — a display name that exists only in flight.

Every other domain value is deliberately identifier-free (spec R6); see
:mod:`blybot.domain.models`, whose contract is that nothing there can hold
a user id, username, or display name. A **relayed** message inverts that
by necessity: a mirror of a conversation is unreadable without knowing who
said what, so ``alice (discord)`` has to survive the crossing.

This is **not** a second carve-out to R6. R6 forbids a display name
reaching Meta, and forbids one reaching disk. A relay does neither:

* it never touches the wiki — a :class:`RelayMessage` becomes an
  :class:`~blybot.domain.models.OutboundMessage` for another chat and
  nothing else;
* it never touches storage — the bridge runs every adapter in one
  process, so relaying is an in-memory call with no queue on disk (that
  constraint is *why* the unified topology was chosen, not a consequence
  of it);
* it never enters the pseudonymized capture layer — an inbound message is
  read once and forks immediately: :class:`CapturedMessage` carries a
  per-channel HMAC label inward, while this carries the real name
  sideways to another transport. The two paths never see each other's
  data.

The type is isolated in its own module so that contract has somewhere to
live and so an architecture guard can assert the boundary: a
``RelayMessage`` must never reach the archive or a wiki sink.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from blybot.domain.models import Scope


@dataclass(frozen=True, slots=True)
class RelayMessage:
    """One message to mirror into every other channel of its bridge.

    ``author`` is the **raw** display name the origin adapter read from
    its SDK, deliberately unformatted: turning it into ``alice (discord)``
    needs ``origin.platform``, which makes it the neutral service's job,
    not something each adapter reimplements.
    """

    origin: Scope
    author: str
    text: str

    def __post_init__(self) -> None:
        if not self.author:
            msg = "RelayMessage.author must be non-empty"
            raise ValueError(msg)
