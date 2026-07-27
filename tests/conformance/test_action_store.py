"""ActionStore contract — every implementation proves these semantics.

Runs against BOTH the in-memory fake and the real ToolsDbStore (over its
SQL-level fake). Round-trip fidelity and "only non-empty scopes are scheduled"
are the port's meaning; an impl that drifts fails here.
"""

from __future__ import annotations

import pytest

from blybot.domain.models import Scope
from blybot.domain.ports import ActionStore
from blybot.services.actions import parse_action
from tests.conformance._impls import ACTION_STORES

SPEC = parse_action("daily@06:00 summarize model=large", now_iso="2026-07-25T12:00:00+00:00")


@pytest.fixture(
    params=[build for _, build in ACTION_STORES], ids=[name for name, _ in ACTION_STORES]
)
def actions(request: pytest.FixtureRequest) -> ActionStore:
    build = request.param
    store: ActionStore = build()
    return store


async def test_unconfigured_scope_has_no_actions(actions: ActionStore) -> None:
    assert await actions.get_actions(Scope("telegram", "-1")) == ()


async def test_set_then_get_actions_round_trips(actions: ActionStore) -> None:
    await actions.set_actions(Scope("telegram", "-1"), (SPEC,))
    assert await actions.get_actions(Scope("telegram", "-1")) == (SPEC,)


async def test_list_scheduled_returns_only_scopes_with_actions(actions: ActionStore) -> None:
    await actions.set_actions(Scope("telegram", "-1"), (SPEC,))
    await actions.set_actions(
        Scope("telegram", "-2"), ()
    )  # explicit empty: stored, never scheduled

    assert await actions.list_scheduled() == [(Scope("telegram", "-1"), (SPEC,))]
