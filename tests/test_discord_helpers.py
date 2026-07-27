"""Discord adapter helpers: capabilities, scope edge, author masker."""

from __future__ import annotations

from blybot.adapters.discord.author_mask import DiscordAuthorMasker
from blybot.adapters.discord.capabilities import DISCORD_CAPABILITIES
from blybot.adapters.discord.scope import discord_target, dm_scope, scope_of
from blybot.domain.models import Scope


def test_capabilities_match_the_platform_contract() -> None:
    caps = DISCORD_CAPABILITIES
    assert caps.max_message_chars == 2000
    assert caps.threads is True
    assert caps.durable_dm is True
    assert caps.deep_links is False
    assert caps.chat_picker is False
    assert caps.message_delete is True
    assert caps.id_can_change is False
    assert caps.rich_choices is True


def test_scope_of_a_plain_channel_has_no_thread() -> None:
    assert scope_of(555000111) == Scope("discord", "555000111", "")


def test_scope_of_a_thread_carries_the_thread_snowflake() -> None:
    assert scope_of(555000111, 999888777) == Scope("discord", "555000111", "999888777")


def test_scope_of_treats_a_zero_thread_as_no_thread() -> None:
    # Defensive: a falsy thread id collapses to the channel default.
    assert scope_of(555000111, 0).thread == ""


def test_dm_scope_uses_the_dm_channel_id() -> None:
    assert dm_scope(424242) == Scope("discord", "424242")


def test_discord_target_round_trips_a_plain_channel() -> None:
    assert discord_target(scope_of(555000111)) == (555000111, None)


def test_discord_target_round_trips_a_thread() -> None:
    assert discord_target(scope_of(555000111, 999888777)) == (555000111, 999888777)


def test_author_mask_is_a_short_stable_hex_label() -> None:
    masker = DiscordAuthorMasker(key="operator-secret")
    label = masker.mask(111, 0, 222)
    assert len(label) == 12
    assert all(char in "0123456789abcdef" for char in label)
    assert masker.mask(111, 0, 222) == label  # stable per (scope, author)


def test_author_mask_differs_across_authors_and_scopes() -> None:
    masker = DiscordAuthorMasker(key="operator-secret")
    base = masker.mask(111, 0, 222)
    assert masker.mask(111, 0, 333) != base  # different author
    assert masker.mask(444, 0, 222) != base  # different channel
    assert masker.mask(111, 55, 222) != base  # different thread
    assert DiscordAuthorMasker(key="other-key").mask(111, 0, 222) != base  # key rotation
