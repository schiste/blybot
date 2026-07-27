"""Digest-subscription grammar and the transient subscribe-target registry.

Pure text ⇄ options parsing for the ``/subscribe`` DM command (reusing the
``/action`` schedule grammar and the ``/llm`` language check), plus
:class:`SubscriptionBinding` — the short-lived, memory-only state that a
tapped subscribe deep link arms so the user's next ``/subscribe`` knows
which scope it is for. The durable half of the link is the scope's
``subscribe_code`` on its profile; nothing identifying is persisted here.
"""

from __future__ import annotations

import secrets
from dataclasses import dataclass, field, replace
from datetime import timedelta
from typing import TYPE_CHECKING, Final

from blybot.domain.models import (
    ActionSpec,
    Schedule,
    StepSpec,
    TriggerKind,
    TriggerSpec,
)
from blybot.domain.ports import StorageError
from blybot.observability import log_event
from blybot.services.actions import ActionParseError, parse_schedule
from blybot.services.llmconf import valid_lang

if TYPE_CHECKING:
    from datetime import datetime

    from blybot.domain.models import OutboundMessage, Scope
    from blybot.domain.ports import Clock, ProfileStore, SubscriptionStore
    from blybot.domain.subscriptions import Subscription
    from blybot.observability import Counters
    from blybot.services.engine import ActionEngine

RECIPES: Final = frozenset({"summarize", "talking_points", "stats"})
_DEFAULT_HOUR: Final = 8


class SubscriptionParseError(Exception):
    """The /subscribe options could not be parsed; the message is user-facing."""


def parse_subscription(text: str, default_lang: str) -> tuple[Schedule, str, str]:
    """Parse ``/subscribe`` options into ``(schedule, recipe, lang)``.

    Tokens are order-free: a schedule token (``daily@HH:MM``, ``every:Nh``,
    ``weekly@<dow>.HH:MM``), a recipe name, and/or ``lang:<code>``.
    Anything omitted defaults to ``daily@08:00``, ``summarize``, and the
    scope's language.
    """
    schedule: Schedule | None = None
    recipe = "summarize"
    lang = default_lang
    for token in text.split():
        if token in RECIPES:
            recipe = token
        elif token.startswith("lang:"):
            value = token[len("lang:") :].lower()
            if not valid_lang(value):
                msg = "lang must be a short language code, e.g. lang:fr"
                raise SubscriptionParseError(msg)
            lang = value
        else:
            schedule = _parse_schedule_token(token)
    return schedule or Schedule(kind="daily", hour=_DEFAULT_HOUR), recipe, lang


def _parse_schedule_token(token: str) -> Schedule:
    try:
        return parse_schedule(token)
    except ActionParseError as error:
        recipes = ", ".join(sorted(RECIPES))
        msg = (
            f"Didn't understand {token!r}. Use a schedule (daily@HH:MM, every:6h, "
            f"weekly@mon.09:00), a recipe ({recipes}), and/or lang:xx."
        )
        raise SubscriptionParseError(msg) from error


def mint_subscribe_code() -> str:
    """A random deep-link capability code marking a scope subscribable."""
    return secrets.token_urlsafe(16)


def mint_sub_id() -> str:
    """A short, human-quotable subscription id (never derived from user data)."""
    return secrets.token_hex(4)


@dataclass(eq=False)
class SubscriptionBinding:
    """Transient "the next /subscribe from this DM is for scope X" state.

    Memory-only with a tight TTL, like :class:`TokenBinding`'s entries; a
    restart just voids in-flight subscribe attempts and the user re-taps
    the (durable) link.
    """

    clock: Clock
    entry_ttl: timedelta = timedelta(minutes=10)
    _entries: dict[Scope, tuple[Scope, datetime]] = field(default_factory=dict)

    def open_entry(self, dm: Scope, source: Scope) -> None:
        """Arm the DM scope's next /subscribe for the ``source`` group scope."""
        self._prune()
        self._entries[dm] = (source, self.clock.now())

    def pending_target(self, dm: Scope) -> Scope | None:
        """Return the scope this DM scope is about to subscribe to, if any."""
        entry = self._entries.get(dm)
        if entry is None:
            return None
        source, opened_at = entry
        if self.clock.now() - opened_at > self.entry_ttl:
            del self._entries[dm]
            return None
        return source

    def close_entry(self, dm: Scope) -> None:
        """Disarm the pending subscribe for this DM scope."""
        self._entries.pop(dm, None)

    def _prune(self) -> None:
        now = self.clock.now()
        self._entries = {
            dm: value for dm, value in self._entries.items() if now - value[1] <= self.entry_ttl
        }


def _recipe_transforms(recipe: str, lang: str) -> tuple[StepSpec, ...]:
    if recipe == "stats":
        return (StepSpec(name="stats"),)  # deterministic, no LLM / language
    return (StepSpec(name="prompt", params=(("template", recipe), ("lang", lang))),)


def _digest_spec(sub: Subscription) -> ActionSpec:
    """Build the one-shot digest pipeline for a subscription.

    ``window=since_last_run`` with ``last_run`` = the subscription's *old*
    watermark tiles the archive exactly-once between deliveries. The sink
    renders to a chat message; the scheduler re-targets it to the DM.
    """
    return ActionSpec(
        action_id="sub",
        trigger=TriggerSpec(kind=TriggerKind.COMMAND, command="subscription"),
        source=StepSpec(name="archive_window", params=(("window", "since_last_run"),)),
        transforms=_recipe_transforms(sub.recipe, sub.lang),
        sink=StepSpec(name="telegram_reply"),
        last_run=sub.last_run,
    )


@dataclass(eq=False)
class SubscriptionScheduler:
    """Runs due digest subscriptions each tick, delivering privately.

    A :class:`~blybot.adapters.telegram.app.MessageCollector`. Mirrors
    :class:`~blybot.services.schedule.ActionScheduler`: baseline unstamped
    rows, select due, **stamp the watermark durably before delivering**
    (a crash-after-send reads as "already ran", never a duplicate DM), and
    isolate per-subscription failures. The digest runs on the *group*
    scope; only the final re-wrap carries the subscriber's DM chat id, so
    ``ActionContext`` stays identifier-free.
    """

    subscriptions: SubscriptionStore
    profiles: ProfileStore
    engine: ActionEngine
    clock: Clock
    counters: Counters
    max_per_tick: int = 200

    async def collect(self) -> list[OutboundMessage]:
        """Return the DM digests for every due subscription this tick."""
        try:
            subs = await self.subscriptions.list_all()
        except StorageError:
            self.counters.increment("subscription_ticks_failed")
            log_event("subscription_tick", "error")
            return []
        now = self.clock.now()
        messages: list[OutboundMessage] = []
        for sub in subs[: self.max_per_tick]:
            try:
                messages.extend(await self._run_one(sub, now))
            except Exception:
                # Per-subscription isolation: one broken scope/adapter must
                # not abort the tick. Deliberately broad.
                self.counters.increment("subscription_ticks_failed")
                log_event("subscription_tick", "error")
        return messages

    async def _run_one(self, sub: Subscription, now: datetime) -> list[OutboundMessage]:
        if sub.last_run is None:  # baseline: next slot, never a replay
            await self.subscriptions.stamp(sub.sub_id, now)
            return []
        if not sub.schedule.is_due(now, sub.last_run):
            return []
        profile = await self.profiles.get(sub.scope)
        if profile is None or profile.subscribe_code is None:
            return []  # admin revoked subscriptions for this scope — skip, don't stamp
        # Durable watermark before the side effect (no duplicate DM on crash).
        await self.subscriptions.stamp(sub.sub_id, now)
        outcome = await self.engine.run(sub.scope, _digest_spec(sub), now)
        self.counters.increment("subscription_digests")
        return [replace(message, scope=sub.dm) for message in outcome.messages]
