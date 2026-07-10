"""SessionRegistry tests (spec R4, section 10)."""

from __future__ import annotations

from datetime import timedelta

from blybot.services.sessions import SessionRegistry
from tests.fakes import FakeClock, SequentialPseudonyms

TTL = timedelta(minutes=45)


def make_registry(clock: FakeClock) -> SessionRegistry:
    return SessionRegistry(pseudonyms=SequentialPseudonyms(), clock=clock, ttl=TTL)


def test_first_contact_mints_a_session() -> None:
    registry = make_registry(FakeClock())
    session = registry.touch(chat_id=111)
    assert session.pseudonym.value == "Anon-1"


def test_activity_within_ttl_keeps_the_same_identity() -> None:
    clock = FakeClock()
    registry = make_registry(clock)
    first = registry.touch(chat_id=111)
    clock.advance(timedelta(minutes=44))
    again = registry.touch(chat_id=111)
    assert again.pseudonym == first.pseudonym


def test_activity_keeps_refreshing_the_ttl() -> None:
    """The timeout is inactivity-based, not an absolute session lifetime."""
    clock = FakeClock()
    registry = make_registry(clock)
    first = registry.touch(chat_id=111)
    for _ in range(4):
        clock.advance(timedelta(minutes=30))
        session = registry.touch(chat_id=111)
    assert session.pseudonym == first.pseudonym


def test_expiry_mints_a_fresh_identity() -> None:
    clock = FakeClock()
    registry = make_registry(clock)
    first = registry.touch(chat_id=111)
    clock.advance(TTL)
    after = registry.touch(chat_id=111)
    assert after.pseudonym != first.pseudonym


def test_explicit_reset_forces_a_new_identity() -> None:
    """/start always yields a fresh pseudonym (spec section 10)."""
    registry = make_registry(FakeClock())
    first = registry.touch(chat_id=111)
    fresh = registry.reset(chat_id=111)
    assert fresh.pseudonym != first.pseudonym


def test_concurrent_chats_get_distinct_identities() -> None:
    registry = make_registry(FakeClock())
    a = registry.touch(chat_id=111)
    b = registry.touch(chat_id=222)
    assert a.pseudonym != b.pseudonym


def test_sweep_drops_only_expired_sessions() -> None:
    clock = FakeClock()
    registry = make_registry(clock)
    registry.touch(chat_id=111)
    clock.advance(timedelta(minutes=30))
    registry.touch(chat_id=222)
    clock.advance(timedelta(minutes=20))  # 111 is now 50min idle, 222 is 20min idle

    assert registry.sweep() == 1
    fresh = registry.touch(chat_id=111)
    kept = registry.touch(chat_id=222)
    assert fresh.pseudonym.value == "Anon-3"
    assert kept.pseudonym.value == "Anon-2"
