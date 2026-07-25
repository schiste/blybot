"""Action grammar, schedule math, and serialization round-trips."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from blybot.domain.models import Schedule, StepSpec, TriggerKind, TriggerSpec
from blybot.services.actions import (
    ActionParseError,
    describe_action,
    dumps_actions,
    loads_actions,
    parse_action,
    parse_schedule,
)

NOW = datetime(2026, 7, 25, 12, 0, tzinfo=UTC)  # a Saturday


# --- schedule parsing ---------------------------------------------------


@pytest.mark.parametrize("token", ["every:6h", "daily@06:00", "weekly@mon.09:30"])
def test_schedule_tokens_round_trip(token: str) -> None:
    assert parse_schedule(token).token == token


@pytest.mark.parametrize(
    "token",
    ["every:0h", "every:h", "daily@25:00", "daily@0600", "weekly@xyz.10:00", "weekly@mon",
     "hourly", "daily", ""],
)
def test_bad_schedule_tokens_are_rejected_with_guidance(token: str) -> None:
    with pytest.raises(ActionParseError):
        parse_schedule(token)


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"kind": "monthly"}, "unknown schedule kind"),
        ({"kind": "every"}, "positive hour interval"),
        ({"kind": "daily", "hour": 24}, "valid HH:MM"),
        ({"kind": "weekly", "weekday": 7}, "weekday"),
    ],
)
def test_schedule_validates_its_fields(kwargs: dict[str, int | str], message: str) -> None:
    with pytest.raises(ValueError, match=message):
        Schedule(**kwargs)  # type: ignore[arg-type]


# --- due-time math ------------------------------------------------------


def test_every_schedule_is_due_after_the_interval_elapses() -> None:
    schedule = parse_schedule("every:6h")
    assert not schedule.is_due(NOW, last_run=NOW - timedelta(hours=5, minutes=59))
    assert schedule.is_due(NOW, last_run=NOW - timedelta(hours=6))


def test_daily_schedule_fires_once_per_slot() -> None:
    schedule = parse_schedule("daily@06:00")
    slot = NOW.replace(hour=6, minute=0)
    assert schedule.is_due(NOW, last_run=slot - timedelta(hours=1))  # slot passed since
    assert not schedule.is_due(NOW, last_run=slot)  # already ran this slot
    assert not schedule.is_due(slot - timedelta(minutes=5), last_run=slot - timedelta(days=1))


def test_daily_schedule_makes_up_a_missed_slot_exactly_once() -> None:
    schedule = parse_schedule("daily@06:00")
    last_run = NOW - timedelta(days=3)  # downtime spanning three slots
    assert schedule.is_due(NOW, last_run=last_run)
    # After the make-up run, nothing more is owed until tomorrow's slot.
    assert not schedule.is_due(NOW + timedelta(hours=1), last_run=NOW)


def test_weekly_schedule_uses_the_configured_weekday() -> None:
    schedule = parse_schedule("weekly@mon.09:00")  # NOW is a Saturday
    monday_nine = NOW - timedelta(days=5, hours=3)
    assert schedule.is_due(NOW, last_run=monday_nine - timedelta(minutes=1))
    assert not schedule.is_due(NOW, last_run=monday_nine)


def test_weekly_slot_later_today_wraps_to_last_week() -> None:
    schedule = parse_schedule("weekly@sat.23:00")  # today, but later than NOW
    last_week = NOW - timedelta(days=7)
    assert schedule.is_due(NOW, last_run=last_week - timedelta(hours=2))
    assert not schedule.is_due(NOW, last_run=NOW - timedelta(days=1))


# --- model validation ---------------------------------------------------


def test_trigger_spec_requires_matching_payload() -> None:
    with pytest.raises(ValueError, match="command name"):
        TriggerSpec(kind=TriggerKind.COMMAND)
    with pytest.raises(ValueError, match="need a schedule"):
        TriggerSpec(kind=TriggerKind.SCHEDULE)


def test_step_spec_requires_a_name_and_defaults_params() -> None:
    with pytest.raises(ValueError, match="non-empty"):
        StepSpec(name="")
    step = StepSpec(name="prompt", params=(("template", "summarize"),))
    assert step.param("template") == "summarize"
    assert step.param("model", "default") == "default"


# --- /action add parsing ------------------------------------------------


def test_parse_action_routes_params_to_their_steps() -> None:
    spec = parse_action(
        "daily@06:00 summarize window=24h page=Meta:Log model=large lang=fr",
        now_iso=NOW.isoformat(),
    )
    assert spec.trigger.kind is TriggerKind.SCHEDULE
    assert spec.trigger.schedule == parse_schedule("daily@06:00")
    assert spec.source == StepSpec(name="archive_window", params=(("window", "24h"),))
    (transform,) = spec.transforms
    assert transform.name == "prompt"
    assert transform.param("template") == "summarize"
    assert transform.param("model") == "large"
    assert transform.param("lang") == "fr"
    assert spec.sink == StepSpec(name="wiki_section", params=(("page", "Meta:Log"),))
    assert spec.last_run == NOW  # primed: never fires for slots before creation


def test_parse_action_supports_stats_and_custom_prompt_recipes() -> None:
    stats = parse_action("every:12h stats")
    assert stats.transforms == (StepSpec(name="stats"),)
    custom = parse_action("weekly@fri.17:00 prompt:decision_log")
    (transform,) = custom.transforms
    assert transform.param("template") == "decision_log"


@pytest.mark.parametrize(
    "text",
    [
        "",  # nothing at all
        "daily@06:00",  # schedule without a recipe
        "daily@06:00 unknown_recipe",
        "daily@06:00 prompt:",  # empty template
        "daily@06:00 summarize bogus=1",  # unknown parameter key
        "daily@06:00 summarize model",  # not key=value
    ],
)
def test_parse_action_rejects_bad_payloads(text: str) -> None:
    with pytest.raises(ActionParseError):
        parse_action(text)


def test_action_ids_are_short_and_random() -> None:
    first = parse_action("daily@06:00 summarize")
    second = parse_action("daily@06:00 summarize")
    assert first.action_id != second.action_id
    assert len(first.action_id) == 4


# --- description and storage round-trip ----------------------------------


def test_describe_action_reads_as_one_line() -> None:
    spec = parse_action("daily@06:00 summarize model=large")
    described = describe_action(spec)
    assert described.startswith(f"[{spec.action_id}] daily@06:00: ")
    assert "archive_window → prompt(template=summarize,model=large) → wiki_section" in described


def test_describe_action_renders_command_triggers() -> None:
    spec = loads_actions(
        '[{"id":"ab12","trigger":{"kind":"command","command":"summarize"},'
        '"source":{"name":"archive_window"},"transforms":[],"sink":{"name":"telegram_reply"}}]'
    )[0]
    assert describe_action(spec) == "[ab12] /summarize: archive_window → telegram_reply"


def test_actions_round_trip_through_json() -> None:
    specs = (
        parse_action("daily@06:00 summarize model=large", now_iso=NOW.isoformat()),
        parse_action("weekly@mon.09:00 stats page=Meta:Stats"),
    )
    assert loads_actions(dumps_actions(specs)) == specs
    assert loads_actions(None) == ()
    assert loads_actions("") == ()


def test_command_triggered_actions_round_trip_too() -> None:
    scheduled = parse_action("daily@06:00 summarize")
    spec = replace(
        scheduled,
        trigger=TriggerSpec(kind=TriggerKind.COMMAND, command="summarize"),
    )
    assert loads_actions(dumps_actions((spec,))) == (spec,)
