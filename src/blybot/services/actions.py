"""Parsing, describing, and serializing configurable actions.

Pure text ⇄ :class:`~blybot.domain.models.ActionSpec` translation. No
I/O, no Telegram — everything here is deterministic and exhaustively
unit-tested, mirroring :mod:`blybot.services.rules`.

Action grammar (``/action add`` payload)::

    <schedule> <recipe> [key=value ...]

* ``schedule`` is ``every:<N>h``, ``daily@HH:MM``, or
  ``weekly@<dow>.HH:MM`` (``dow`` = mon…sun), all UTC.
* ``recipe`` names a built-in pipeline: ``summarize``,
  ``talking_points``, ``stats``, or ``prompt:<template>`` for any named
  prompt template.
* ``key=value`` parameters attach to the step that understands them:
  ``window=<N>h|<N>d`` (source), ``page=<title>`` (sink), and
  ``model=default|large``, ``lang=<code>``, ``temp=<0..1>`` (prompt
  transform).
"""

from __future__ import annotations

import json
import re
import secrets
from datetime import datetime
from typing import Any, Final

from blybot.domain.models import (
    WEEKDAY_TOKENS,
    ActionSpec,
    Schedule,
    StepSpec,
    TriggerKind,
    TriggerSpec,
)

MAX_ACTIONS: Final = 20

_RECIPES: Final = ("summarize", "talking_points", "stats")
_RECIPE_HELP: Final = ", ".join((*_RECIPES, "stats_narrative", "prompt:<template>"))

# Which pipeline step each key=value parameter belongs to.
_SOURCE_KEYS: Final = frozenset({"window"})
_SINK_KEYS: Final = frozenset({"page"})
_TRANSFORM_KEYS: Final = frozenset({"model", "lang", "temp"})

_TIME_RE: Final = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")
_EVERY_RE: Final = re.compile(r"^every:(\d+)h$")
_WINDOW_SUGAR_RE: Final = re.compile(r"^\d+[hd]$")


class ActionParseError(Exception):
    """The action text could not be parsed; the message is user-facing."""


def parse_schedule(token: str) -> Schedule:
    """Parse one schedule token into a :class:`Schedule`.

    Raises :class:`ActionParseError` with an actionable message on bad
    input.
    """
    if match := _EVERY_RE.match(token):
        hours = int(match.group(1))
        if hours == 0:
            msg = "every:<N>h needs N of at least 1"
            raise ActionParseError(msg)
        return Schedule(kind="every", interval_hours=hours)
    kind, sep, rest = token.partition("@")
    if sep and kind == "daily":
        return Schedule(kind="daily", **_parse_time(rest))
    if sep and kind == "weekly":
        day, dot, time = rest.partition(".")
        if dot and day in WEEKDAY_TOKENS:
            return Schedule(kind="weekly", weekday=WEEKDAY_TOKENS.index(day), **_parse_time(time))
        msg = f"weekly@ needs <dow>.HH:MM with dow one of {', '.join(WEEKDAY_TOKENS)}"
        raise ActionParseError(msg)
    msg = f"Unknown schedule {token!r}. Use every:<N>h, daily@HH:MM, or weekly@<dow>.HH:MM (UTC)"
    raise ActionParseError(msg)


def parse_action(text: str, now_iso: str | None = None) -> ActionSpec:
    """Parse an ``/action add`` payload into an :class:`ActionSpec`.

    ``now_iso`` (an ISO-8601 UTC instant) primes ``last_run`` so a new
    schedule never fires for slots that predate its creation.
    """
    tokens = text.split()
    if len(tokens) < 2:  # noqa: PLR2004 -- a schedule and a recipe
        msg = f"Usage: /action add <schedule> <recipe> [key=value …]. Recipes: {_RECIPE_HELP}"
        raise ActionParseError(msg)
    schedule = parse_schedule(tokens[0])
    source, transforms, sink = _resolve_recipe(tokens[1])
    params = _parse_params(tokens[2:])
    return ActionSpec(
        action_id=_mint_id(),
        trigger=TriggerSpec(kind=TriggerKind.SCHEDULE, schedule=schedule),
        source=_with_params(source, params, _SOURCE_KEYS),
        transforms=tuple(_with_params(step, params, _TRANSFORM_KEYS) for step in transforms),
        sink=_with_params(sink, params, _SINK_KEYS),
        last_run=_parse_instant(now_iso),
    )


def command_action(command: str, recipe: str, arg_tokens: list[str]) -> ActionSpec:
    """Build the one-shot :class:`ActionSpec` behind an on-demand command.

    ``arg_tokens`` uses the same ``key=value`` grammar as ``/action add``,
    plus a bare leading window token (``24h``/``7d``) as sugar for
    ``window=…``.
    """
    tokens = list(arg_tokens)
    if tokens and _WINDOW_SUGAR_RE.match(tokens[0]):
        tokens[0] = f"window={tokens[0]}"
    source, transforms, sink = _resolve_recipe(recipe)
    params = _parse_params(tokens)
    return ActionSpec(
        action_id=_mint_id(),
        trigger=TriggerSpec(kind=TriggerKind.COMMAND, command=command),
        source=_with_params(source, params, _SOURCE_KEYS),
        transforms=tuple(_with_params(step, params, _TRANSFORM_KEYS) for step in transforms),
        sink=_with_params(sink, params, _SINK_KEYS),
    )


def describe_action(spec: ActionSpec) -> str:
    """A one-line human description of an action for ``/action list``."""
    trigger = (
        spec.trigger.schedule.token
        if spec.trigger.schedule is not None
        else f"/{spec.trigger.command}"
    )
    steps = " → ".join(_describe_step(step) for step in (spec.source, *spec.transforms, spec.sink))
    return f"[{spec.action_id}] {trigger}: {steps}"


def dumps_actions(actions: tuple[ActionSpec, ...]) -> str:
    """Serialize a scope's actions (state included) to compact JSON."""
    return json.dumps([_action_to_dict(spec) for spec in actions], separators=(",", ":"))


def loads_actions(text: str | None) -> tuple[ActionSpec, ...]:
    """Rebuild a scope's actions from stored JSON (``None``/empty → none)."""
    if not text:
        return ()
    return tuple(_action_from_dict(item) for item in json.loads(text))


def _parse_time(value: str) -> dict[str, int]:
    if match := _TIME_RE.match(value):
        return {"hour": int(match.group(1)), "minute": int(match.group(2))}
    msg = f"Expected a UTC time as HH:MM, got {value!r}"
    raise ActionParseError(msg)


def _resolve_recipe(recipe: str) -> tuple[StepSpec, tuple[StepSpec, ...], StepSpec]:
    name, sep, template = recipe.partition(":")
    transforms: tuple[StepSpec, ...]
    if name == "prompt" and sep and template:
        transforms = (StepSpec(name="prompt", params=(("template", template),)),)
    elif recipe == "stats":
        transforms = (StepSpec(name="stats"),)
    elif recipe == "stats_narrative":
        # The narrative template consumes computed numbers, never raw
        # transcript — so the recipe chains the stats transform first.
        transforms = (
            StepSpec(name="stats"),
            StepSpec(name="prompt", params=(("template", "stats_narrative"),)),
        )
    elif recipe in _RECIPES:
        transforms = (StepSpec(name="prompt", params=(("template", recipe),)),)
    else:
        msg = f"Unknown recipe {recipe!r}. Recipes: {_RECIPE_HELP}"
        raise ActionParseError(msg)
    return StepSpec(name="archive_window"), transforms, StepSpec(name="wiki_section")


def _parse_params(tokens: list[str]) -> dict[str, str]:
    known = _SOURCE_KEYS | _SINK_KEYS | _TRANSFORM_KEYS
    params: dict[str, str] = {}
    for token in tokens:
        key, sep, value = token.partition("=")
        if not sep or not value or key not in known:
            keys = ", ".join(sorted(known))
            msg = f"Expected key=value with a key from: {keys}; got {token!r}"
            raise ActionParseError(msg)
        _validate_param(key, value)
        params[key] = value
    return params


def _validate_param(key: str, value: str) -> None:
    """Reject bad values at add time, not at 06:00 tomorrow."""
    if key == "window" and not _WINDOW_SUGAR_RE.match(value):
        msg = f"window must look like 24h or 7d, got {value!r}"
        raise ActionParseError(msg)
    if key == "model" and value not in {"default", "large"}:
        msg = "model must be default or large"
        raise ActionParseError(msg)
    if key == "lang" and not (value.replace("-", "").isalpha() and len(value) <= 8):  # noqa: PLR2004
        msg = "lang must be a short language code, e.g. lang=fr"
        raise ActionParseError(msg)
    if key == "temp":
        try:
            temperature = float(value)
        except ValueError as error:
            msg = "temp must be a number between 0 and 1"
            raise ActionParseError(msg) from error
        if not 0.0 <= temperature <= 1.0:
            msg = "temp must be between 0 and 1"
            raise ActionParseError(msg)


def _with_params(step: StepSpec, params: dict[str, str], keys: frozenset[str]) -> StepSpec:
    picked = tuple((key, params[key]) for key in sorted(keys & params.keys()))
    if not picked:
        return step
    return StepSpec(name=step.name, params=(*step.params, *picked))


def _describe_step(step: StepSpec) -> str:
    if not step.params:
        return step.name
    rendered = ",".join(f"{key}={value}" for key, value in step.params)
    return f"{step.name}({rendered})"


def _action_to_dict(spec: ActionSpec) -> dict[str, Any]:
    trigger: dict[str, Any] = {"kind": spec.trigger.kind.value}
    if spec.trigger.schedule is not None:
        trigger["schedule"] = spec.trigger.schedule.token
    if spec.trigger.command:
        trigger["command"] = spec.trigger.command
    data: dict[str, Any] = {
        "id": spec.action_id,
        "trigger": trigger,
        "source": _step_to_dict(spec.source),
        "transforms": [_step_to_dict(step) for step in spec.transforms],
        "sink": _step_to_dict(spec.sink),
    }
    if spec.last_run is not None:
        data["last_run"] = spec.last_run.isoformat()
    return data


def _step_to_dict(step: StepSpec) -> dict[str, Any]:
    data: dict[str, Any] = {"name": step.name}
    if step.params:
        data["params"] = [list(pair) for pair in step.params]
    return data


def _action_from_dict(item: dict[str, Any]) -> ActionSpec:
    trigger = item["trigger"]
    schedule = parse_schedule(trigger["schedule"]) if "schedule" in trigger else None
    return ActionSpec(
        action_id=item["id"],
        trigger=TriggerSpec(
            kind=TriggerKind(trigger["kind"]),
            command=trigger.get("command", ""),
            schedule=schedule,
        ),
        source=_step_from_dict(item["source"]),
        transforms=tuple(_step_from_dict(step) for step in item["transforms"]),
        sink=_step_from_dict(item["sink"]),
        last_run=_parse_instant(item.get("last_run")),
    )


def _step_from_dict(item: dict[str, Any]) -> StepSpec:
    params = tuple((key, value) for key, value in item.get("params", ()))
    return StepSpec(name=item["name"], params=params)


def _parse_instant(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


def _mint_id() -> str:
    """A short, human-quotable action id (never derived from user data)."""
    return secrets.token_hex(2)
