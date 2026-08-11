"""The neutral relay core: fan-out, attribution, and the echo fence (#77)."""

from __future__ import annotations

import pytest

from blybot.domain.bridge import RelayMessage
from blybot.domain.models import Scope
from blybot.observability import Counters
from blybot.services.bridge import BridgeService, build_groups

TG = Scope("telegram", "-100500")
DC = Scope("discord", "900100")
IRC = Scope("irc", "#wikipedia-fr")


def _service(*groups: frozenset[Scope]) -> BridgeService:
    return BridgeService(groups=groups or (frozenset({TG, DC, IRC}),), counters=Counters())


def test_a_message_reaches_every_other_channel_but_not_its_own() -> None:
    messages = _service().relay(RelayMessage(origin=DC, author="alice", text="hello"))

    assert [message.scope for message in messages] == [IRC, TG]  # sorted, origin excluded
    assert {message.text for message in messages} == {"alice (discord): hello"}


def test_attribution_names_the_origin_platform_not_the_destination() -> None:
    """`alice (discord)` read on IRC has to say where alice actually is."""
    (message,) = _service(frozenset({DC, IRC})).relay(
        RelayMessage(origin=DC, author="alice", text="hi")
    )
    assert message.scope == IRC
    assert message.text == "alice (discord): hi"


def test_a_scope_in_no_bridge_relays_nowhere() -> None:
    service = _service(frozenset({TG, DC}))
    assert service.targets(IRC) == ()
    assert service.relay(RelayMessage(origin=IRC, author="someone", text="hi")) == ()


def test_the_bots_own_output_is_never_relayed_again() -> None:
    """A relayed line fed back in must not make a second lap.

    A bouncer echo or a second bot instance can do that; without the fence
    the attribution prefix compounds on every lap.
    """
    service = BridgeService(
        groups=(frozenset({TG, DC, IRC}),),
        counters=Counters(),
        bot_authors={"irc": "blybot", "discord": "Blybot"},
    )

    echoed = service.relay(RelayMessage(origin=IRC, author="blybot", text="alice (discord): hi"))
    assert echoed == ()
    assert service.counters.snapshot()["bridge_echo_ignored"] == 1

    # Case-folded: IRC nicks and Discord display names both vary in case.
    assert service.relay(RelayMessage(origin=DC, author="blybot", text="x")) == ()


def test_a_human_quoting_the_relay_format_is_still_relayed() -> None:
    """The fence is *who wrote it*, not what it looks like.

    Display names contain spaces, so no anchored pattern can separate
    "Alice Smith (discord): hi" from "I saw alice (discord): hi". Sniffing
    the text would silently swallow the second one.
    """
    service = BridgeService(
        groups=(frozenset({TG, DC, IRC}),),
        counters=Counters(),
        bot_authors={"irc": "blybot"},
    )
    quoted = service.relay(
        RelayMessage(origin=IRC, author="bob", text="I saw alice (discord): hi earlier")
    )
    assert len(quoted) == 2

    spaced = service.relay(RelayMessage(origin=DC, author="Alice Smith", text="hi"))
    assert {message.text for message in spaced} == {"Alice Smith (discord): hi"}


def test_an_empty_or_whitespace_message_is_not_relayed() -> None:
    service = _service()
    assert service.relay(RelayMessage(origin=DC, author="alice", text="   ")) == ()


def test_long_text_is_left_whole_for_the_transport_to_split() -> None:
    """Chunking here would only decide where the prefix repeats; the IRC
    transport already splits by encoded bytes at the protocol limit."""
    (message,) = _service(frozenset({DC, IRC})).relay(
        RelayMessage(origin=DC, author="alice", text="x" * 4000)
    )
    assert message.text == f"alice (discord): {'x' * 4000}"


def test_a_scope_cannot_belong_to_two_bridges() -> None:
    """Two groups sharing a scope reintroduce the cycle the set shape prevents."""
    with pytest.raises(ValueError, match="more than one bridge"):
        BridgeService(
            groups=(frozenset({TG, DC}), frozenset({DC, IRC})),
            counters=Counters(),
        )


def test_relaying_is_symmetric_across_every_member() -> None:
    """Whoever speaks, everyone else hears it — that is what a mirror means."""
    service = _service()
    for origin in (TG, DC, IRC):
        messages = service.relay(RelayMessage(origin=origin, author="a", text="hi"))
        assert {message.scope for message in messages} == {TG, DC, IRC} - {origin}


def test_build_groups_drops_a_group_with_nothing_to_mirror() -> None:
    assert build_groups([[TG], [TG, DC], []]) == (frozenset({TG, DC}),)


def test_a_relay_message_needs_an_author() -> None:
    """An unattributable line would mirror as a bare, sourceless message."""
    with pytest.raises(ValueError, match="author must be non-empty"):
        RelayMessage(origin=DC, author="", text="hi")
