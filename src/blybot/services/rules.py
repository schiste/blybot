"""Parsing, describing, and formatting composable event rules.

Pure text ⇄ :class:`~blybot.domain.models.Rule` translation plus the
per-event-type message formatting. No I/O, no Telegram, no GitHub —
everything here is deterministic and exhaustively unit-tested.

Rule grammar (``/rule add`` payload)::

    <trigger> [key:value ...] [live|digest]

* ``trigger`` is an :class:`EventType` token, e.g. ``pr.merged``.
* filter keys: ``label:`` (repeatable / comma = any-of), ``author:``,
  ``base:``, ``assignee:``, ``milestone:``, ``draft:true|false``, and
  ``title:`` (substring, or ``title:/regex/`` for a pattern).
* trailing ``live`` (default) or ``digest`` sets the delivery mode.
"""

from __future__ import annotations

import re
import secrets
from typing import TYPE_CHECKING, Final

from blybot.domain.models import DeliveryMode, EventType, Resource, Rule, RuleFilter

if TYPE_CHECKING:
    from collections.abc import Iterable

    from blybot.domain.models import RepoEvent

MAX_RULES: Final = 20

_TRIGGERS: Final = ", ".join(member.token for member in EventType)


class RuleParseError(Exception):
    """The rule text could not be parsed; the message is user-facing."""


def parse_rule(text: str) -> Rule:
    """Parse a ``/rule add`` payload into a :class:`Rule`.

    Raises :class:`RuleParseError` with an actionable message on bad
    input. Delivery mode defaults to ``live``.
    """
    tokens = text.split()
    if not tokens:
        msg = f"Give an event type: one of {_TRIGGERS}"
        raise RuleParseError(msg)

    try:
        trigger = EventType.from_token(tokens[0])
    except ValueError as error:
        msg = f"Unknown event type {tokens[0]!r}. Try one of: {_TRIGGERS}"
        raise RuleParseError(msg) from error

    mode = DeliveryMode.LIVE
    conditions: dict[str, str] = {}
    for token in tokens[1:]:
        if token in {DeliveryMode.LIVE.value, DeliveryMode.DIGEST.value}:
            mode = DeliveryMode(token)
            continue
        key, sep, value = token.partition(":")
        if not sep or not value:
            msg = f"Expected key:value, got {token!r} (e.g. label:bug, base:main)"
            raise RuleParseError(msg)
        conditions[key] = f"{conditions[key]},{value}" if key in conditions else value

    return Rule(rule_id=_mint_id(), trigger=trigger, filter=_build_filter(conditions), mode=mode)


def describe_rule(rule: Rule) -> str:
    """A one-line human description of a rule for ``/rules``."""
    parts = [rule.trigger.token]
    parts.extend(_describe_filter(rule.filter))
    parts.append(f"→ {rule.mode.value}")
    return f"[{rule.rule_id}] {' '.join(parts)}"


def resources_for(rules: Iterable[Rule]) -> set[Resource]:
    """The set of GitHub resource streams these rules need polled."""
    return {rule.trigger.resource for rule in rules}


def format_event(event: RepoEvent) -> str:
    """Render one matched event as a chat line."""
    label = _EVENT_LABELS[event.event_type]
    who = f" by {event.author}" if event.author else ""
    return f"{label}: {event.title}{who} {event.url}".rstrip()


_EVENT_LABELS: Final = {
    EventType.ISSUE_OPENED: "Issue opened",
    EventType.ISSUE_CLOSED: "Issue closed",
    EventType.ISSUE_REOPENED: "Issue reopened",
    EventType.ISSUE_LABELED: "Issue labeled",
    EventType.ISSUE_ASSIGNED: "Issue assigned",
    EventType.ISSUE_MILESTONED: "Issue milestoned",
    EventType.PR_OPENED: "PR opened",
    EventType.PR_CLOSED: "PR closed",
    EventType.PR_MERGED: "PR merged",
    EventType.PR_READY: "PR ready for review",
    EventType.COMMENT: "Comment",
    EventType.RELEASE: "Release",
}

_VALID_KEYS: Final = frozenset(
    {"label", "author", "base", "assignee", "milestone", "draft", "title"}
)


def _build_filter(conditions: dict[str, str]) -> RuleFilter:
    unknown = set(conditions) - _VALID_KEYS
    if unknown:
        keys = ", ".join(sorted(_VALID_KEYS))
        msg = f"Unknown filter {sorted(unknown)[0]!r}. Valid keys: {keys}"
        raise RuleParseError(msg)
    draft = _parse_draft(conditions["draft"]) if "draft" in conditions else None
    title, title_is_regex = _parse_title(conditions.get("title", ""))
    return RuleFilter(
        labels=frozenset(_split(conditions.get("label", ""))),
        author=conditions.get("author", ""),
        base=conditions.get("base", ""),
        assignee=conditions.get("assignee", ""),
        milestone=conditions.get("milestone", ""),
        title_match=title,
        title_is_regex=title_is_regex,
        draft=draft,
    )


def _parse_draft(value: str) -> bool:
    if value not in {"true", "false"}:
        msg = "draft must be true or false"
        raise RuleParseError(msg)
    return value == "true"


def _parse_title(value: str) -> tuple[str, bool]:
    if len(value) >= 2 and value.startswith("/") and value.endswith("/"):  # noqa: PLR2004
        pattern = value[1:-1]
        try:
            re.compile(pattern)
        except re.error as error:
            msg = f"Invalid title regex: {error}"
            raise RuleParseError(msg) from error
        return pattern, True
    return value, False


def _split(value: str) -> list[str]:
    return [part for part in value.split(",") if part]


def _describe_filter(rule_filter: RuleFilter) -> list[str]:
    parts: list[str] = []
    if rule_filter.labels:
        parts.append(f"label:{','.join(sorted(rule_filter.labels))}")
    for name, value in (
        ("author", rule_filter.author),
        ("base", rule_filter.base),
        ("assignee", rule_filter.assignee),
        ("milestone", rule_filter.milestone),
    ):
        if value:
            parts.append(f"{name}:{value}")
    if rule_filter.title_match:
        rendered = (
            f"/{rule_filter.title_match}/"
            if rule_filter.title_is_regex
            else rule_filter.title_match
        )
        parts.append(f"title:{rendered}")
    if rule_filter.draft is not None:
        parts.append(f"draft:{str(rule_filter.draft).lower()}")
    return parts


def _mint_id() -> str:
    """A short, human-quotable rule id (never derived from user data)."""
    return secrets.token_hex(2)
