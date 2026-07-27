"""Subscription grammar + pending-target binding tests."""

from __future__ import annotations

from datetime import timedelta

import pytest

from blybot.domain.models import Scope
from blybot.services.subscriptions import (
    SubscriptionBinding,
    SubscriptionParseError,
    mint_sub_id,
    mint_subscribe_code,
    parse_subscription,
)
from tests.fakes import FakeClock


def test_defaults_when_no_options() -> None:
    schedule, recipe, lang = parse_subscription("", "fr")
    assert schedule.token == "daily@08:00"  # noqa: S105 -- schedule token
    assert recipe == "summarize"
    assert lang == "fr"


def test_parses_schedule_recipe_and_lang_in_any_order() -> None:
    schedule, recipe, lang = parse_subscription("stats lang:de weekly@mon.09:00", "en")
    assert schedule.token == "weekly@mon.09:00"  # noqa: S105 -- schedule token
    assert recipe == "stats"
    assert lang == "de"


@pytest.mark.parametrize("text", ["lang:zzzz1", "nonsense@99:99", "every:0h"])
def test_rejects_bad_options(text: str) -> None:
    with pytest.raises(SubscriptionParseError):
        parse_subscription(text, "en")


def test_mint_helpers_are_nonempty_and_fresh() -> None:
    assert mint_subscribe_code() != mint_subscribe_code()
    assert mint_sub_id() != mint_sub_id()
    assert len(mint_sub_id()) == 8  # token_hex(4) → 8 hex chars


def test_binding_arms_and_reads_pending_targets() -> None:
    clock = FakeClock()
    binding = SubscriptionBinding(clock=clock)
    binding.open_entry(Scope("telegram", "500"), Scope("telegram", "-100", "7"))
    binding.open_entry(Scope("telegram", "501"), Scope("telegram", "-200"))  # prune keeps fresh 500
    assert binding.pending_target(Scope("telegram", "500")) == Scope("telegram", "-100", "7")
    assert binding.pending_target(Scope("telegram", "501")) == Scope("telegram", "-200")
    binding.close_entry(Scope("telegram", "500"))
    assert binding.pending_target(Scope("telegram", "500")) is None  # closed → gone
    assert binding.pending_target(Scope("telegram", "999")) is None  # never armed → None


def test_binding_entry_expires_on_peek() -> None:
    clock = FakeClock()
    binding = SubscriptionBinding(clock=clock, entry_ttl=timedelta(minutes=10))
    binding.open_entry(Scope("telegram", "500"), Scope("telegram", "-100"))
    clock.advance(timedelta(minutes=11))
    assert binding.pending_target(Scope("telegram", "500")) is None  # TTL-expired peek evicts it


def test_binding_prunes_expired_entries_on_open() -> None:
    clock = FakeClock()
    binding = SubscriptionBinding(clock=clock, entry_ttl=timedelta(minutes=10))
    binding.open_entry(Scope("telegram", "500"), Scope("telegram", "-100"))
    clock.advance(timedelta(minutes=11))
    binding.open_entry(
        Scope("telegram", "501"), Scope("telegram", "-200")
    )  # _prune drops stale 500
    assert Scope("telegram", "500") not in binding._entries
    assert binding.pending_target(Scope("telegram", "501")) == Scope("telegram", "-200")
