"""Subscription value-object tests (the R6 DM-chat-id carve-out)."""

from __future__ import annotations

import pytest

from blybot.domain.models import GroupProfile, Schedule, Scope
from blybot.domain.ports import StorageError
from blybot.domain.subscriptions import Subscription
from blybot.services.subscriptions import (
    MAX_SUBS_PER_USER,
    SubscriptionCapReachedError,
    admit_subscription,
)
from tests.fakes import InMemorySubscriptions


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


# --- per-user admission cap (issue #23) --------------------------------------


def _capped_sub(dm: Scope, index: int, scope: Scope | None = None) -> Subscription:
    # sub_ids are globally unique in production (secrets.token_hex), and the
    # store is keyed by them — so the fixture must not collide across
    # subscribers or one user's rows would silently overwrite another's.
    return Subscription(
        sub_id=f"{dm.channel}-{index:02d}",
        dm=dm,
        scope=scope or Scope("telegram", "-100"),
        schedule=Schedule(kind="daily", hour=8),
        recipe="summarize",
        lang="en",
    )


async def test_the_cap_admits_up_to_n_then_refuses_the_next() -> None:
    store = InMemorySubscriptions()
    dm = Scope("telegram", "777")
    for index in range(MAX_SUBS_PER_USER):
        await admit_subscription(store, _capped_sub(dm, index))
    assert len(store.subs) == MAX_SUBS_PER_USER

    with pytest.raises(SubscriptionCapReachedError) as refused:
        await admit_subscription(store, _capped_sub(dm, 99))
    assert str(MAX_SUBS_PER_USER) in str(refused.value)  # the message names the cap
    assert "/unsubscribe" in str(refused.value)  # …and how to free one up
    assert len(store.subs) == MAX_SUBS_PER_USER  # nothing was written


async def test_the_cap_is_per_subscriber_not_global() -> None:
    """One abuser at their ceiling must not lock everyone else out."""
    store = InMemorySubscriptions()
    hog, other = Scope("telegram", "777"), Scope("telegram", "888")
    for index in range(MAX_SUBS_PER_USER):
        await admit_subscription(store, _capped_sub(hog, index))
    with pytest.raises(SubscriptionCapReachedError):
        await admit_subscription(store, _capped_sub(hog, 99))

    await admit_subscription(store, _capped_sub(other, 0))  # a different DM is unaffected
    assert len(await store.list_for_user(other)) == 1
    # And the hog's existing subscriptions are untouched — still deliverable.
    assert len(await store.list_for_user(hog)) == MAX_SUBS_PER_USER


async def test_freeing_a_slot_lets_the_next_one_in() -> None:
    store = InMemorySubscriptions()
    dm = Scope("telegram", "777")
    for index in range(MAX_SUBS_PER_USER):
        await admit_subscription(store, _capped_sub(dm, index))
    assert await store.remove(dm, f"{dm.channel}-00")

    await admit_subscription(store, _capped_sub(dm, 99))  # no longer refused
    assert len(await store.list_for_user(dm)) == MAX_SUBS_PER_USER


async def test_a_custom_cap_is_honoured_and_storage_errors_propagate() -> None:
    store = InMemorySubscriptions()
    dm = Scope("telegram", "777")
    await admit_subscription(store, _capped_sub(dm, 0), max_per_user=1)
    with pytest.raises(SubscriptionCapReachedError):
        await admit_subscription(store, _capped_sub(dm, 1), max_per_user=1)

    store.fail = True
    with pytest.raises(StorageError):  # an outage stays an outage, not a refusal
        await admit_subscription(store, _capped_sub(Scope("telegram", "999"), 0))
