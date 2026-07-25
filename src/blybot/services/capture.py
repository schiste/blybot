"""Channel/group message capture (v3 plan §2.7).

The single policy boundary between the update stream and storage: a
message is archived only when its scope has capture explicitly enabled.
Everything below this line never runs for a non-captured scope. The
service also applies the volume guards — text truncation and a
per-scope ingest ceiling — and swallows storage hiccups so capture can
never make the bot lag its interactive commands.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Final

from blybot.domain.models import OutboundMessage
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

    async def ingest(self, message: CapturedMessage) -> None:
        """Archive ``message`` iff its scope opted in; never raises."""
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
        self._enabled_cache.pop((chat_id, thread_id), None)

    async def _enabled(self, chat_id: int, thread_id: int) -> bool:
        key = (chat_id, thread_id)
        now = self.clock.now()
        cached = self._enabled_cache.get(key)
        if cached is not None and cached[1] > now:
            return cached[0]
        try:
            profile = await self.store.get(chat_id, thread_id)
            # Forum topics inherit the group default (thread 0), the same
            # two-tier resolution the directory applies to pages/repos —
            # /capture on in General covers every topic.
            if not (profile and profile.capture_enabled) and thread_id:
                profile = await self.store.get(chat_id, 0)
        except StorageError:
            return False  # fail closed, and never cache an outage
        enabled = bool(profile and profile.capture_enabled)
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
