"""GroupPolicy and SlidingWindowLimiter tests (spec 8, 15; N4)."""

from __future__ import annotations

from datetime import timedelta

from blybot.services.policy import GroupPolicy, SlidingWindowLimiter
from tests.fakes import FakeClock


class TestGroupPolicy:
    def test_empty_allowlist_serves_any_group(self) -> None:
        assert GroupPolicy(allowed=set()).is_allowed(-100123)

    def test_non_empty_allowlist_restricts(self) -> None:
        policy = GroupPolicy(allowed={-100123})
        assert policy.is_allowed(-100123)
        assert not policy.is_allowed(-100999)

    def test_supergroup_migration_rewrites_the_reference(self) -> None:
        policy = GroupPolicy(allowed={-123})
        assert policy.migrate(-123, -100456)
        assert not policy.is_allowed(-123)
        assert policy.is_allowed(-100456)

    def test_migration_of_unlisted_group_is_a_noop(self) -> None:
        policy = GroupPolicy(allowed={-1})
        assert not policy.migrate(-2, -3)
        assert policy.allowed == {-1}


class TestSlidingWindowLimiter:
    def make(self, clock: FakeClock, limit: int = 3) -> SlidingWindowLimiter:
        return SlidingWindowLimiter(clock=clock, limit=limit, window=timedelta(minutes=1))

    def test_allows_up_to_the_cap(self) -> None:
        limiter = self.make(FakeClock())
        assert [limiter.allow("group", 1) for _ in range(4)] == [True, True, True, False]

    def test_window_slides(self) -> None:
        clock = FakeClock()
        limiter = self.make(clock)
        for _ in range(3):
            limiter.allow("group", 1)
        assert not limiter.allow("group", 1)
        clock.advance(timedelta(seconds=61))
        assert limiter.allow("group", 1)

    def test_keys_are_independent(self) -> None:
        limiter = self.make(FakeClock(), limit=1)
        assert limiter.allow("group", 1)
        assert limiter.allow("user", 1)  # same id, different scope
        assert limiter.allow("group", 2)
        assert not limiter.allow("group", 1)

    def test_rejected_attempts_do_not_extend_the_window(self) -> None:
        clock = FakeClock()
        limiter = self.make(clock, limit=1)
        assert limiter.allow("group", 1)
        clock.advance(timedelta(seconds=59))
        assert not limiter.allow("group", 1)
        clock.advance(timedelta(seconds=2))
        assert limiter.allow("group", 1)
