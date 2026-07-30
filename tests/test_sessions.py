"""SessionRegistry tests (spec R4, section 10)."""

from __future__ import annotations

import logging
from datetime import timedelta

import pytest

from blybot.domain.models import Scope
from blybot.observability import Counters
from blybot.services.sessions import SessionRegistry, SessionSweeper
from tests.fakes import FakeClock, ScriptedPseudonyms, SequentialPseudonyms

TTL = timedelta(minutes=45)


def dm(chat_id: int) -> Scope:
    return Scope("telegram", str(chat_id))


def make_registry(clock: FakeClock) -> SessionRegistry:
    return SessionRegistry(pseudonyms=SequentialPseudonyms(), clock=clock, ttl=TTL)


def test_first_contact_mints_a_session() -> None:
    registry = make_registry(FakeClock())
    session = registry.touch(dm(111))
    assert session.pseudonym.value == "Anon-1"


def test_activity_within_ttl_keeps_the_same_identity() -> None:
    clock = FakeClock()
    registry = make_registry(clock)
    first = registry.touch(dm(111))
    clock.advance(timedelta(minutes=44))
    again = registry.touch(dm(111))
    assert again.pseudonym == first.pseudonym


def test_activity_keeps_refreshing_the_ttl() -> None:
    """The timeout is inactivity-based, not an absolute session lifetime."""
    clock = FakeClock()
    registry = make_registry(clock)
    first = registry.touch(dm(111))
    for _ in range(4):
        clock.advance(timedelta(minutes=30))
        session = registry.touch(dm(111))
    assert session.pseudonym == first.pseudonym


def test_expiry_mints_a_fresh_identity() -> None:
    clock = FakeClock()
    registry = make_registry(clock)
    first = registry.touch(dm(111))
    clock.advance(TTL)
    after = registry.touch(dm(111))
    assert after.pseudonym != first.pseudonym


def test_explicit_reset_forces_a_new_identity() -> None:
    """/start always yields a fresh pseudonym (spec section 10)."""
    registry = make_registry(FakeClock())
    first = registry.touch(dm(111))
    fresh = registry.reset(dm(111))
    assert fresh.pseudonym != first.pseudonym


def test_concurrent_chats_get_distinct_identities() -> None:
    registry = make_registry(FakeClock())
    a = registry.touch(dm(111))
    b = registry.touch(dm(222))
    assert a.pseudonym != b.pseudonym


def test_advance_counts_messages_within_a_session() -> None:
    registry = make_registry(FakeClock())
    first = registry.advance(dm(111))
    second = registry.advance(dm(111))
    assert (first.message_count, second.message_count) == (1, 2)
    assert second.pseudonym == first.pseudonym


def test_touch_preserves_the_message_count() -> None:
    registry = make_registry(FakeClock())
    registry.advance(dm(111))
    assert registry.touch(dm(111)).message_count == 1


def test_expiry_resets_the_message_count() -> None:
    clock = FakeClock()
    registry = make_registry(clock)
    registry.advance(dm(111))
    clock.advance(TTL)
    assert registry.advance(dm(111)).message_count == 1


def test_sweep_drops_only_expired_sessions() -> None:
    clock = FakeClock()
    registry = make_registry(clock)
    registry.touch(dm(111))
    clock.advance(timedelta(minutes=30))
    registry.touch(dm(222))
    clock.advance(timedelta(minutes=20))  # 111 is now 50min idle, 222 is 20min idle

    assert registry.sweep() == 1
    fresh = registry.touch(dm(111))
    kept = registry.touch(dm(222))
    assert fresh.pseudonym.value == "Anon-3"
    assert kept.pseudonym.value == "Anon-2"


def test_minting_avoids_pseudonyms_of_other_known_sessions() -> None:
    """Two concurrent discussions must never share a section heading."""
    clock = FakeClock()
    registry = SessionRegistry(
        pseudonyms=ScriptedPseudonyms(values=["Same", "Same", "Other"]), clock=clock, ttl=TTL
    )
    first = registry.touch(dm(1))
    second = registry.touch(dm(2))  # first draw collides, re-draws
    assert first.anchor == "Same"
    assert second.anchor == "Other"


def test_reset_never_returns_the_current_identity() -> None:
    registry = SessionRegistry(
        pseudonyms=ScriptedPseudonyms(values=["Same", "Same", "Fresh"]),
        clock=FakeClock(),
        ttl=TTL,
    )
    registry.touch(dm(1))
    assert registry.reset(dm(1)).anchor == "Fresh"


def test_exhausted_redraw_budget_degrades_to_a_repeat() -> None:
    """A pathologically tiny namespace repeats rather than looping forever."""
    registry = SessionRegistry(
        pseudonyms=ScriptedPseudonyms(values=["Only"]), clock=FakeClock(), ttl=TTL
    )
    registry.touch(dm(1))
    assert registry.touch(dm(2)).anchor == "Only"


async def test_the_sweeper_evicts_and_reports_exactly_like_telegrams_tick(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Regression guard: peek() expires a session logically, so an unswept
    registry keeps BEHAVING right while the dict grows — and _mint draws its
    collision set from every entry it holds, live or dead. Discord has no
    maintenance tick, so without this sweeper minting degrades toward reusing
    a pseudonym and two discussions could share a section heading."""
    clock = FakeClock()
    registry = SessionRegistry(
        pseudonyms=SequentialPseudonyms(), clock=clock, ttl=timedelta(minutes=30)
    )
    counters = Counters()
    sweeper = SessionSweeper(sessions=registry, counters=counters)

    registry.touch(Scope("discord", "1"))
    registry.touch(Scope("discord", "2"))
    assert await sweeper.collect() == []  # never emits messages
    assert len(registry._sessions) == 2  # nothing expired yet
    assert counters.snapshot().get("sessions_expired") is None  # and nothing logged

    clock.advance(timedelta(hours=1))
    with caplog.at_level(logging.INFO, logger="blybot"):
        assert await sweeper.collect() == []
    assert registry._sessions == {}  # the dict actually shrinks
    assert counters.snapshot()["sessions_expired"] == 2
    assert any("event=session_sweep outcome=ok expired=2" in m for m in caplog.messages)
