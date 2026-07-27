"""Subscription value-object tests (the R6 DM-chat-id carve-out)."""

from __future__ import annotations

import pytest

from blybot.domain.models import GroupProfile, Schedule
from blybot.domain.subscriptions import Subscription


def _sub(**over: object) -> Subscription:
    base: dict[str, object] = {
        "sub_id": "ab12",
        "dm_chat_id": 555,
        "chat_id": -100,
        "thread_id": 0,
        "schedule": Schedule(kind="daily", hour=8),
        "recipe": "summarize",
        "lang": "en",
    }
    base.update(over)
    return Subscription(**base)  # type: ignore[arg-type]


def test_subscription_holds_the_dm_chat_id_and_scope() -> None:
    sub = _sub()
    assert sub.dm_chat_id == 555
    assert (sub.chat_id, sub.thread_id) == (-100, 0)
    assert sub.last_run is None


@pytest.mark.parametrize(("field", "value"), [("sub_id", ""), ("recipe", ""), ("lang", "")])
def test_subscription_rejects_empty_required_fields(field: str, value: str) -> None:
    with pytest.raises(ValueError, match="must be non-empty"):
        _sub(**{field: value})


def test_group_profile_subscribe_code_defaults_to_none() -> None:
    assert GroupProfile(chat_id=-1).subscribe_code is None
