"""Volatile private-DM route selection state."""

from __future__ import annotations

from datetime import timedelta

from blybot.domain.models import Scope
from blybot.services.dm_routing import DmRouteRegistry, PendingDmMessages
from tests.fakes import FakeClock


def dm(chat_id: int) -> Scope:
    return Scope("telegram", str(chat_id))


def grp(chat_id: int) -> Scope:
    return Scope("telegram", str(chat_id))


def test_pending_message_round_trips_for_matching_request() -> None:
    routes = PendingDmMessages(clock=FakeClock())
    request_id = routes.open_pending(dm(1), "hello")
    assert routes.pop_pending(dm(1), request_id + 1) is None
    assert routes.pop_pending(dm(1), request_id) == "hello"
    assert routes.pop_pending(dm(1), request_id) is None


def test_request_ids_wrap_inside_a_signed_32_bit_range() -> None:
    routes = PendingDmMessages(clock=FakeClock())
    routes._next_request_id = 2**31 - 1
    request_id = routes.open_pending(dm(1), "hello")
    assert request_id == 2**31 - 1
    assert routes.open_pending(dm(2), "next") == 1


def test_pending_message_expires() -> None:
    clock = FakeClock()
    routes = PendingDmMessages(clock=clock)
    request_id = routes.open_pending(dm(1), "hello")
    clock.advance(timedelta(minutes=6))
    assert routes.pop_pending(dm(1), request_id) is None


def test_routes_expire_and_can_be_refreshed() -> None:
    clock = FakeClock()
    routes = DmRouteRegistry(clock=clock, route_ttl=timedelta(minutes=45))
    routes.save_route(dm(1), grp(-100), "Project/Telegram logs")
    clock.advance(timedelta(minutes=30))
    routes.touch_route(dm(1))
    clock.advance(timedelta(minutes=30))

    route = routes.route_for(dm(1))
    assert route is not None
    assert route.page == "Project/Telegram logs"

    clock.advance(timedelta(minutes=45))
    assert routes.route_for(dm(1)) is None
    routes.touch_route(dm(1))  # no-op after expiry


def test_prune_discards_stale_state_on_each_side_of_the_split() -> None:
    """Routes and parked messages expire independently now — they are separate
    concerns with separate TTLs (#45)."""
    clock = FakeClock()
    routes = DmRouteRegistry(clock=clock, route_ttl=timedelta(minutes=45))
    pending = PendingDmMessages(clock=clock)
    pending.open_pending(dm(1), "old")
    routes.save_route(dm(1), grp(-100), "Old")
    clock.advance(timedelta(hours=1))

    pending.open_pending(dm(2), "new")
    assert pending._pending.keys() == {dm(2)}
    routes.save_route(dm(2), grp(-200), "New")
    assert routes.route_for(dm(1)) is None
    assert routes._routes.keys() == {dm(2)}
