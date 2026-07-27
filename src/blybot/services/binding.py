"""Nonce-bound token entry flow (spec v2, Phase B).

A group admin runs ``/setrepo``; the bot mints a one-time nonce baked
into a deep link. Tapping it fires ``/start cfg_<nonce>`` in DM, where
the nonce is redeemed, admin-ship of *that group* is re-verified live,
and a short-lived pending entry opens: the admin's next private message
is treated as the group's API token — validated, encrypted, stored —
instead of being transcribed.

Nothing here persists. Nonces and pending entries are memory-only with
tight TTLs and hold only chat ids; a restart simply voids in-flight
links, and the admin taps a fresh one.
"""

from __future__ import annotations

import secrets
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from blybot.domain.models import Scope
    from blybot.domain.ports import Clock


@dataclass(eq=False)
class TokenBinding:
    """One-time deep-link nonces and pending DM token entries."""

    clock: Clock
    link_ttl: timedelta = timedelta(minutes=10)
    entry_ttl: timedelta = timedelta(minutes=5)
    _links: dict[str, tuple[Scope, datetime]] = field(default_factory=dict)
    _entries: dict[Scope, tuple[Scope, datetime]] = field(default_factory=dict)

    def mint_link(self, group: Scope) -> str:
        """Return a fresh one-time nonce bound to the ``group`` scope."""
        self._prune()
        nonce = secrets.token_urlsafe(9)
        self._links[nonce] = (group, self.clock.now())
        return nonce

    def peek_link(self, nonce: str) -> Scope | None:
        """Return the nonce's group scope without consuming it."""
        entry = self._links.get(nonce)
        if entry is None:
            return None
        group, minted_at = entry
        if self.clock.now() - minted_at > self.link_ttl:
            return None
        return group

    def redeem_link(self, nonce: str) -> Scope | None:
        """Consume the nonce; return its group scope if still fresh."""
        entry = self._links.pop(nonce, None)
        if entry is None:
            return None
        group, minted_at = entry
        if self.clock.now() - minted_at > self.link_ttl:
            return None
        return group

    def open_entry(self, dm: Scope, group: Scope) -> None:
        """Arm token entry: the DM scope's next message is a token for ``group``."""
        self._prune()
        self._entries[dm] = (group, self.clock.now())

    def pending_target(self, dm: Scope) -> Scope | None:
        """Return the group scope awaiting a token from this DM scope, if any.

        Peeks without consuming: a rejected token may be retried until
        the entry expires or :meth:`close_entry` runs on success.
        """
        entry = self._entries.get(dm)
        if entry is None:
            return None
        group, opened_at = entry
        if self.clock.now() - opened_at > self.entry_ttl:
            del self._entries[dm]
            return None
        return group

    def close_entry(self, dm: Scope) -> None:
        """Disarm token entry for this DM scope."""
        self._entries.pop(dm, None)

    def _prune(self) -> None:
        now = self.clock.now()
        self._links = {
            nonce: value for nonce, value in self._links.items() if now - value[1] <= self.link_ttl
        }
        self._entries = {
            dm: value for dm, value in self._entries.items() if now - value[1] <= self.entry_ttl
        }
