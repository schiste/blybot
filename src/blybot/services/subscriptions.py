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
from dataclasses import dataclass, field
from datetime import timedelta
from typing import TYPE_CHECKING, Final

from blybot.domain.models import Schedule
from blybot.services.actions import ActionParseError, parse_schedule
from blybot.services.llmconf import valid_lang

if TYPE_CHECKING:
    from datetime import datetime

    from blybot.domain.ports import Clock

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
    _entries: dict[int, tuple[int, int, datetime]] = field(default_factory=dict)

    def open_entry(self, dm_chat_id: int, chat_id: int, thread_id: int) -> None:
        """Arm the DM chat's next /subscribe for the (group, topic) scope."""
        self._prune()
        self._entries[dm_chat_id] = (chat_id, thread_id, self.clock.now())

    def pending_target(self, dm_chat_id: int) -> tuple[int, int] | None:
        """Return the scope this DM chat is about to subscribe to, if any."""
        entry = self._entries.get(dm_chat_id)
        if entry is None:
            return None
        chat_id, thread_id, opened_at = entry
        if self.clock.now() - opened_at > self.entry_ttl:
            del self._entries[dm_chat_id]
            return None
        return chat_id, thread_id

    def close_entry(self, dm_chat_id: int) -> None:
        """Disarm the pending subscribe for this DM chat."""
        self._entries.pop(dm_chat_id, None)

    def _prune(self) -> None:
        now = self.clock.now()
        self._entries = {
            dm: value for dm, value in self._entries.items() if now - value[2] <= self.entry_ttl
        }
