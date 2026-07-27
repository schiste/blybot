"""Unit tests for the :class:`Scope` value object."""

from __future__ import annotations

import pytest

from blybot.domain.models import Scope


def test_valid_construction_carries_all_three_parts() -> None:
    scope = Scope(platform="telegram", channel="-100500", thread="7")
    assert scope.platform == "telegram"
    assert scope.channel == "-100500"
    assert scope.thread == "7"


def test_thread_defaults_to_empty() -> None:
    scope = Scope("telegram", "-100500")
    assert scope.thread == ""


def test_key_joins_parts_on_reserved_separators() -> None:
    assert Scope("telegram", "-100500", "7").key == "telegram:-100500/7"
    # The channel default (no thread) still renders a stable, collision-free key.
    assert Scope("telegram", "-100500").key == "telegram:-100500/"


def test_empty_platform_is_rejected() -> None:
    with pytest.raises(ValueError, match="platform must be non-empty"):
        Scope("", "-100500")


def test_empty_channel_is_rejected() -> None:
    with pytest.raises(ValueError, match="channel must be non-empty"):
        Scope("telegram", "")


@pytest.mark.parametrize(
    ("platform", "channel", "thread"),
    [
        ("tele:gram", "-100500", ""),  # ':' in platform
        ("telegram", "-100:500", ""),  # ':' in channel
        ("telegram", "-100500", "7:8"),  # ':' in thread
    ],
)
def test_colon_in_any_part_is_rejected(platform: str, channel: str, thread: str) -> None:
    with pytest.raises(ValueError, match="must not contain"):
        Scope(platform, channel, thread)


@pytest.mark.parametrize(
    ("platform", "channel", "thread"),
    [
        ("tele/gram", "-100500", ""),  # '/' in platform
        ("telegram", "-100/500", ""),  # '/' in channel
        ("telegram", "-100500", "7/8"),  # '/' in thread
    ],
)
def test_slash_in_any_part_is_rejected(platform: str, channel: str, thread: str) -> None:
    with pytest.raises(ValueError, match="must not contain"):
        Scope(platform, channel, thread)


def test_scope_is_hashable_and_usable_as_a_dict_key() -> None:
    a = Scope("telegram", "1", "2")
    b = Scope("telegram", "1", "2")
    registry = {a: "value"}
    assert registry[b] == "value"  # value equality → same key
