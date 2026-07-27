"""Subscription value-object tests (the R6 DM-chat-id carve-out)."""

from __future__ import annotations

import pytest

from blybot.domain.models import GroupProfile, Schedule, Scope
from blybot.domain.subscriptions import Subscription


def _sub(**over: object) -> Subscription:
    base: dict[str, object] = {
        "sub_id": "ab12",
        "dm": Scope("telegram", "555"),
        "scope": Scope("telegram", "-100"),
        "schedule": Schedule(kind="daily", hour=8),
        "recipe": "summarize",
        "lang": "en",
    }
    base.update(over)
    return Subscription(**base)  # type: ignore[arg-type]


def test_subscription_holds_the_dm_chat_id_and_scope() -> None:
    sub = _sub()
    assert int(sub.dm.channel) == 555
    assert (int(sub.scope.channel), int(sub.scope.thread) if sub.scope.thread else 0) == (-100, 0)
    assert sub.last_run is None


@pytest.mark.parametrize(("field", "value"), [("sub_id", ""), ("recipe", ""), ("lang", "")])
def test_subscription_rejects_empty_required_fields(field: str, value: str) -> None:
    with pytest.raises(ValueError, match="must be non-empty"):
        _sub(**{field: value})


def test_group_profile_subscribe_code_defaults_to_none() -> None:
    assert GroupProfile(scope=Scope("telegram", "-1")).subscribe_code is None
