"""ProfileStore contract — every implementation proves these semantics.

Runs against BOTH the in-memory fake and the real ToolsDbStore (over its
SQL-level fake). Any impl that drifts from the port's meaning fails here.
"""

from __future__ import annotations

import pytest

from blybot.domain.models import GroupProfile, Scope
from blybot.domain.ports import ProfileStore
from tests.conformance._impls import PROFILE_STORES


@pytest.fixture(
    params=[build for _, build in PROFILE_STORES], ids=[name for name, _ in PROFILE_STORES]
)
def profiles(request: pytest.FixtureRequest) -> ProfileStore:
    build = request.param
    store: ProfileStore = build()
    return store


async def test_upsert_then_get_round_trips(profiles: ProfileStore) -> None:
    profile = GroupProfile(scope=Scope("telegram", "-100"), log_page="Logs/Here", repo="o/r")
    await profiles.upsert(profile)
    assert await profiles.get(Scope("telegram", "-100")) == profile


async def test_get_of_an_absent_scope_is_none(profiles: ProfileStore) -> None:
    assert await profiles.get(Scope("telegram", "-404")) is None


async def test_delete_removes_the_profile(profiles: ProfileStore) -> None:
    await profiles.upsert(GroupProfile(scope=Scope("telegram", "-100")))
    await profiles.delete(Scope("telegram", "-100"))
    assert await profiles.get(Scope("telegram", "-100")) is None


async def test_list_event_enabled_returns_only_enabled_in_stable_order(
    profiles: ProfileStore,
) -> None:
    # Inserted in channel-sorted order so the two ordering guarantees (the
    # adapter sorts; the fake preserves insertion) agree on one sequence.
    await profiles.upsert(GroupProfile(scope=Scope("telegram", "-100"), events_enabled=True))
    await profiles.upsert(GroupProfile(scope=Scope("telegram", "-200"), events_enabled=True))
    await profiles.upsert(GroupProfile(scope=Scope("telegram", "-300"), events_enabled=False))

    listed = await profiles.list_event_enabled()

    assert [p.scope for p in listed] == [Scope("telegram", "-100"), Scope("telegram", "-200")]
    assert listed == await profiles.list_event_enabled()  # stable across calls


async def test_list_capture_enabled_returns_only_enabled_in_stable_order(
    profiles: ProfileStore,
) -> None:
    await profiles.upsert(GroupProfile(scope=Scope("telegram", "-100"), capture_enabled=True))
    await profiles.upsert(GroupProfile(scope=Scope("telegram", "-200"), capture_enabled=True))
    await profiles.upsert(GroupProfile(scope=Scope("telegram", "-300"), capture_enabled=False))
    await profiles.upsert(GroupProfile(scope=Scope("telegram", "-400")))  # tri-state None

    listed = await profiles.list_capture_enabled()

    assert [p.scope for p in listed] == [Scope("telegram", "-100"), Scope("telegram", "-200")]
    assert listed == await profiles.list_capture_enabled()


async def test_migrate_rekeys_by_channel_and_clears_a_colliding_destination(
    profiles: ProfileStore,
) -> None:
    await profiles.upsert(GroupProfile(scope=Scope("telegram", "-100"), log_page="Dest/old"))
    await profiles.upsert(GroupProfile(scope=Scope("telegram", "-900"), log_page="Source"))
    await profiles.upsert(
        GroupProfile(scope=Scope("telegram", "-900", "5"), log_page="Source/topic")
    )

    await profiles.migrate(Scope("telegram", "-900"), Scope("telegram", "-100"))

    assert await profiles.get(Scope("telegram", "-900")) is None  # source moved off its channel
    moved = await profiles.get(Scope("telegram", "-100"))
    assert moved is not None
    assert moved.log_page == "Source"  # authoritative source overwrote the destination
    topic = await profiles.get(Scope("telegram", "-100", "5"))
    assert topic is not None
    assert topic.log_page == "Source/topic"  # every topic re-keyed too


async def test_cursors_round_trip_and_respect_the_repo_guard(profiles: ProfileStore) -> None:
    scope = Scope("telegram", "-100")
    await profiles.upsert(GroupProfile(scope=scope, repo="owner/repo"))
    assert await profiles.get_cursors(scope) == {}

    cursors = {"issues": "2026-01-01T00:00:00Z", "pulls": "2026-01-02T00:00:00Z"}
    await profiles.set_cursors(scope, cursors, "owner/repo")
    assert await profiles.get_cursors(scope) == cursors

    # An in-flight write for a stale binding is dropped, not applied.
    await profiles.set_cursors(scope, {"issues": "stale"}, "other/repo")
    assert await profiles.get_cursors(scope) == cursors
