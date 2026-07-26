"""Channel/group message capture (v3 plan §2.7).

The single policy boundary between the update stream and storage: a
message is archived only when its scope has capture explicitly enabled.
Everything below this line never runs for a non-captured scope. The
service also applies the volume guards — text truncation and a
per-scope ingest ceiling — and swallows storage hiccups so capture can
never make the bot lag its interactive commands.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Final

from blybot.domain.models import GroupProfile, OutboundMessage
from blybot.domain.ports import StorageError

if TYPE_CHECKING:
    from blybot.domain.models import CapturedMessage
    from blybot.domain.ports import Clock, MessageArchive, ProfileStore
    from blybot.observability import Counters
    from blybot.services.policy import GroupPolicy, SlidingWindowLimiter

MAX_TEXT_CHARS: Final = 4096  # Telegram's own message cap; nothing longer is stored


@dataclass(eq=False)
class CaptureService:
    """Ingests one captured message per call, policy first."""

    store: ProfileStore
    archive: MessageArchive
    limiter: SlidingWindowLimiter
    clock: Clock
    counters: Counters
    cache_ttl: timedelta = timedelta(seconds=60)
    # (chat_id, thread_id) → (capture_enabled, valid_until). High-volume
    # scopes must not cost one profile read per message.
    _enabled_cache: dict[tuple[int, int], tuple[bool, datetime]] = field(
        default_factory=dict, init=False
    )
    # Fail-closed tombstones: scopes whose consent was revoked but whose
    # durable disable write failed (e.g. a channel demotion during a
    # ToolsDB outage). A denied scope is never archived, and each of its
    # messages retries the durable disable until one lands.
    _denied: set[tuple[int, int]] = field(default_factory=set, init=False)
    # Per-chat cache epoch, bumped by every invalidation (forget_scope /
    # deny). An `_enabled` read snapshots it before awaiting the store and
    # refuses to cache if it changed meanwhile — so a consent revocation
    # that lands mid-read can never be re-poisoned by the stale value.
    _epoch: dict[int, int] = field(default_factory=dict, init=False)
    # Serializes consent transitions (enable_scope vs. the tombstone
    # retries): a stale in-flight disable must never overwrite a freshly
    # announced enable, and a check-then-write guard alone is TOCTOU.
    _transition_lock: asyncio.Lock = field(default_factory=asyncio.Lock, init=False)

    async def ingest(self, message: CapturedMessage) -> None:
        """Archive ``message`` iff its scope opted in; never raises."""
        key = (message.chat_id, message.thread_id)
        if key in self._denied:
            await self._retry_disable(key)
            return  # a denied scope is never archived, converged or not
        if not await self._enabled(message.chat_id, message.thread_id):
            return
        # Throttle per (chat, topic) — the documented per-scope ceiling.
        # One busy topic must not starve its siblings' archives.
        if not self.limiter.allow(f"capture:{message.thread_id}", message.chat_id):
            self.counters.increment("captures_throttled")
            return
        if len(message.text) > MAX_TEXT_CHARS:
            message = replace(message, text=message.text[:MAX_TEXT_CHARS])
        try:
            await self.archive.store(message)
        except StorageError:
            self.counters.increment("captures_failed")
            return
        self.counters.increment("captures")

    def forget_scope(self, chat_id: int, thread_id: int) -> None:
        """Drop the scope's cached policy so /capture changes apply promptly."""
        # Bump the chat's epoch first: any `_enabled` read now mid-await
        # will see the change and decline to cache its (now stale) result.
        self._epoch[chat_id] = self._epoch.get(chat_id, 0) + 1
        if thread_id:
            self._enabled_cache.pop((chat_id, thread_id), None)
            return
        # A group-default change must reach every topic that inherited
        # it — otherwise messages keep archiving for up to the TTL after
        # the admin was told capture is off.
        for key in [k for k in self._enabled_cache if k[0] == chat_id]:
            del self._enabled_cache[key]

    def deny_scope(self, chat_id: int, thread_id: int) -> None:
        """Force the scope off in memory until a durable disable lands.

        For consent revocations whose storage write failed: the stale
        durable ``True`` must not resume archiving when storage recovers,
        so the scope is tombstoned and every subsequent message retries
        the disable instead of being stored.
        """
        self._denied.add((chat_id, thread_id))
        self.forget_scope(chat_id, thread_id)

    def clear_denial(self, chat_id: int, thread_id: int) -> None:
        """Lift a tombstone after a durable, verified state transition."""
        self._denied.discard((chat_id, thread_id))
        self.forget_scope(chat_id, thread_id)

    async def enable_scope(self, chat_id: int, thread_id: int) -> None:
        """Durably enable capture, serialized against pending disable retries.

        Holding the transition lock guarantees no stale tombstone retry
        is mid-write when the fresh consent lands, and the write itself
        cancels any pending revocation. Raises :class:`StorageError` on
        failure, with the tombstone (if any) left in place — the caller
        decides how to fail safely.
        """
        async with self._transition_lock:
            profile = await self.store.get(chat_id, thread_id) or GroupProfile(
                chat_id=chat_id, thread_id=thread_id
            )
            await self.store.upsert(replace(profile, capture_enabled=True))
            self._denied.discard((chat_id, thread_id))
        self.forget_scope(chat_id, thread_id)

    async def retry_denied(self) -> None:
        """Converge every tombstoned scope's disable (maintenance tick).

        Runs independently of message arrival, so a quiet channel's
        pending revocation becomes durable within one tick of storage
        recovering — not only when its next post happens to arrive.
        """
        for key in list(self._denied):
            await self._retry_disable(key)

    async def _retry_disable(self, key: tuple[int, int]) -> None:
        """Try to make a denied scope's disable durable; keep denying on failure."""
        chat_id, thread_id = key
        async with self._transition_lock:
            if key not in self._denied:
                # A fresh announced enable landed first: the revocation
                # is cancelled, never clobber the new state.
                return
            try:
                profile = await self.store.get(chat_id, thread_id)
                if profile is not None and profile.capture_enabled:
                    await self.store.upsert(replace(profile, capture_enabled=False))
            except StorageError:
                return  # storage still down: the tombstone stays
            self._denied.discard(key)
            # Bust the cache so the freshly durable `False` is served at
            # once, not up to the TTL later (symmetry with enable_scope).
            self.forget_scope(chat_id, thread_id)

    async def _enabled(self, chat_id: int, thread_id: int) -> bool:
        key = (chat_id, thread_id)
        now = self.clock.now()
        cached = self._enabled_cache.get(key)
        if cached is not None and cached[1] > now:
            return cached[0]
        epoch = self._epoch.get(chat_id, 0)
        try:
            profile = await self.store.get(chat_id, thread_id)
            decision = profile.capture_enabled if profile else None
            # Forum topics inherit the group default (thread 0) only when
            # the topic itself never decided: an explicit /capture off in
            # a topic beats an enabled group, same as any other override.
            if decision is None and thread_id:
                group = await self.store.get(chat_id, 0)
                decision = group.capture_enabled if group else None
        except StorageError:
            return False  # fail closed, and never cache an outage
        enabled = bool(decision)
        if self._epoch.get(chat_id, 0) == epoch:
            # No invalidation landed while we awaited the store, so this
            # read still reflects current consent — safe to cache. If one
            # did (a revocation mid-read), skip the write: it would
            # re-poison the cache the invalidation just cleared.
            self._enabled_cache[key] = (enabled, now + self.cache_ttl)
        return enabled


REMINDER_TEXT: Final = (
    "🔁 Reminder: messages in this chat are being archived for on-wiki "
    "summaries and statistics (authors appear only as anonymous labels). "
    "An admin can stop this with /capture off and erase the archive with "
    "/capture purge."
)


@dataclass(eq=False)
class CaptureReminder:
    """Periodic re-announcement for capture-enabled scopes (v3 §1).

    A :class:`~blybot.adapters.telegram.app.MessageCollector` on the
    shared tick. Cadence state is memory-only on purpose: a restart
    resets every scope's timer, so the worst failure mode is a *late*
    reminder — never a spammed one.
    """

    store: ProfileStore
    groups: GroupPolicy
    clock: Clock
    cadence: timedelta
    _next_due: dict[tuple[int, int], datetime] = field(default_factory=dict, init=False)

    async def collect(self) -> list[OutboundMessage]:
        """Return the reminders due this tick."""
        try:
            profiles = await self.store.list_capture_enabled()
        except StorageError:
            return []
        now = self.clock.now()
        messages: list[OutboundMessage] = []
        for profile in profiles:
            if not self.groups.is_allowed(profile.chat_id):
                continue
            key = (profile.chat_id, profile.thread_id)
            due = self._next_due.get(key)
            if due is None:  # first sighting: schedule, don't repeat the enable announcement
                self._next_due[key] = now + self.cadence
                continue
            if now >= due:
                self._next_due[key] = now + self.cadence
                messages.append(
                    OutboundMessage(
                        chat_id=profile.chat_id,
                        thread_id=profile.thread_id,
                        text=REMINDER_TEXT,
                    )
                )
        return messages
