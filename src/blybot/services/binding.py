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
    from blybot.domain.ports import Clock


@dataclass(eq=False)
class TokenBinding:
    """One-time deep-link nonces and pending DM token entries."""

    clock: Clock
    link_ttl: timedelta = timedelta(minutes=10)
    entry_ttl: timedelta = timedelta(minutes=5)
    _links: dict[str, tuple[int, int, datetime]] = field(default_factory=dict)
    _entries: dict[int, tuple[int, int, datetime]] = field(default_factory=dict)

    def mint_link(self, group_chat_id: int, thread_id: int) -> str:
        """Return a fresh one-time nonce bound to the (group, topic)."""
        self._prune()
        nonce = secrets.token_urlsafe(9)
        self._links[nonce] = (group_chat_id, thread_id, self.clock.now())
        return nonce

    def peek_link(self, nonce: str) -> tuple[int, int] | None:
        """Return the nonce's (group, topic) without consuming it."""
        entry = self._links.get(nonce)
        if entry is None:
            return None
        group_chat_id, thread_id, minted_at = entry
        if self.clock.now() - minted_at > self.link_ttl:
            return None
        return group_chat_id, thread_id

    def redeem_link(self, nonce: str) -> tuple[int, int] | None:
        """Consume the nonce; return its (group, topic) if still fresh."""
        entry = self._links.pop(nonce, None)
        if entry is None:
            return None
        group_chat_id, thread_id, minted_at = entry
        if self.clock.now() - minted_at > self.link_ttl:
            return None
        return group_chat_id, thread_id

    def open_entry(self, dm_chat_id: int, group_chat_id: int, thread_id: int) -> None:
        """Arm token entry: the DM chat's next message is a token for (group, topic)."""
        self._prune()
        self._entries[dm_chat_id] = (group_chat_id, thread_id, self.clock.now())

    def pending_target(self, dm_chat_id: int) -> tuple[int, int] | None:
        """Return the (group, topic) awaiting a token from this DM chat, if any.

        Peeks without consuming: a rejected token may be retried until
        the entry expires or :meth:`close_entry` runs on success.
        """
        entry = self._entries.get(dm_chat_id)
        if entry is None:
            return None
        group_chat_id, thread_id, opened_at = entry
        if self.clock.now() - opened_at > self.entry_ttl:
            del self._entries[dm_chat_id]
            return None
        return group_chat_id, thread_id

    def close_entry(self, dm_chat_id: int) -> None:
        """Disarm token entry for this DM chat."""
        self._entries.pop(dm_chat_id, None)

    def _prune(self) -> None:
        now = self.clock.now()
        self._links = {
            nonce: value for nonce, value in self._links.items() if now - value[2] <= self.link_ttl
        }
        self._entries = {
            dm: value for dm, value in self._entries.items() if now - value[2] <= self.entry_ttl
        }
