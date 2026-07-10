"""TokenBinding tests: one-time nonces and pending entries with tight TTLs."""

from __future__ import annotations

from datetime import timedelta

from blybot.services.binding import TokenBinding
from tests.fakes import FakeClock

GROUP = -100500
DM = 777


def make_binding(clock: FakeClock) -> TokenBinding:
    return TokenBinding(clock=clock)


def test_links_are_one_time() -> None:
    binding = make_binding(FakeClock())
    nonce = binding.mint_link(GROUP)
    assert binding.redeem_link(nonce) == GROUP
    assert binding.redeem_link(nonce) is None  # consumed
    assert binding.redeem_link("bogus") is None


def test_links_expire() -> None:
    clock = FakeClock()
    binding = make_binding(clock)
    nonce = binding.mint_link(GROUP)
    clock.advance(timedelta(minutes=11))
    assert binding.redeem_link(nonce) is None


def test_entries_peek_until_closed() -> None:
    binding = make_binding(FakeClock())
    binding.open_entry(DM, GROUP)
    assert binding.pending_group(DM) == GROUP
    assert binding.pending_group(DM) == GROUP  # peeking does not consume
    binding.close_entry(DM)
    assert binding.pending_group(DM) is None
    binding.close_entry(DM)  # idempotent


def test_entries_expire() -> None:
    clock = FakeClock()
    binding = make_binding(clock)
    binding.open_entry(DM, GROUP)
    clock.advance(timedelta(minutes=6))
    assert binding.pending_group(DM) is None


def test_minting_prunes_stale_state() -> None:
    clock = FakeClock()
    binding = make_binding(clock)
    stale = binding.mint_link(GROUP)
    binding.open_entry(DM, GROUP)
    clock.advance(timedelta(hours=1))
    binding.mint_link(GROUP)  # triggers pruning
    assert stale not in binding._links
    assert DM not in binding._entries


def test_peek_respects_ttl_and_unknown_nonces() -> None:
    clock = FakeClock()
    binding = make_binding(clock)
    nonce = binding.mint_link(GROUP)
    assert binding.peek_link(nonce) == GROUP
    assert binding.peek_link(nonce) == GROUP  # peeking never consumes
    assert binding.peek_link("bogus") is None
    clock.advance(timedelta(minutes=11))
    assert binding.peek_link(nonce) is None
