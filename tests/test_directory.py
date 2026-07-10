"""ChannelDirectory tests: resolution tiers, prefix safety, degradation."""

from __future__ import annotations

import pytest

from blybot.domain.models import ConsentMode, GroupProfile
from blybot.domain.ports import StorageError
from blybot.services.directory import (
    ChannelDirectory,
    PageNotAllowedError,
    SelfServiceUnavailableError,
)
from tests.fakes import InMemoryProfiles

CHAT = -100500


def make_directory(
    store: InMemoryProfiles | None,
    page_prefix: str = "Telegram logs/",
) -> ChannelDirectory:
    return ChannelDirectory(
        store=store,
        default_log_page="Next 25/Telegram logs",
        default_consent=ConsentMode.IMMEDIATE,
        default_repo="schiste/blybot",
        page_prefix=page_prefix,
    )


async def test_v1_mode_without_a_store_resolves_to_defaults() -> None:
    directory = make_directory(store=None)
    settings = await directory.resolve(CHAT)
    assert settings.log_page == "Next 25/Telegram logs"
    assert settings.consent_mode is ConsentMode.IMMEDIATE
    assert not settings.customized
    assert not directory.self_service_enabled


async def test_unconfigured_group_resolves_to_defaults() -> None:
    directory = make_directory(InMemoryProfiles())
    settings = await directory.resolve(CHAT)
    assert settings.log_page == "Next 25/Telegram logs"
    assert not settings.customized


async def test_profile_fields_override_defaults_individually() -> None:
    store = InMemoryProfiles()
    directory = make_directory(store)
    await directory.set_log_page(CHAT, "Telegram logs/Ours")
    settings = await directory.resolve(CHAT)
    assert settings.log_page == "Telegram logs/Ours"
    assert settings.consent_mode is ConsentMode.IMMEDIATE  # unset field: default
    assert settings.customized


async def test_set_consent_is_per_group() -> None:
    directory = make_directory(InMemoryProfiles())
    await directory.set_consent(CHAT, ConsentMode.AUTHOR_ONLY)
    assert (await directory.resolve(CHAT)).consent_mode is ConsentMode.AUTHOR_ONLY
    assert (await directory.resolve(-2)).consent_mode is ConsentMode.IMMEDIATE


async def test_set_log_page_normalizes_underscores_and_whitespace() -> None:
    directory = make_directory(InMemoryProfiles())
    normalized = await directory.set_log_page(CHAT, "Telegram_logs/My  group")
    assert normalized == "Telegram logs/My group"


@pytest.mark.parametrize(
    "title",
    [
        "User talk:Jimbo",  # outside the prefix
        "Telegram logs/",  # prefix alone, no subpage
        "Telegram logs/{{bad}}",  # forbidden characters
        "Telegram logs/" + "x" * 300,  # over MediaWiki's title limit
    ],
)
async def test_pages_outside_the_safe_prefix_are_rejected(title: str) -> None:
    directory = make_directory(InMemoryProfiles())
    with pytest.raises(PageNotAllowedError):
        await directory.set_log_page(CHAT, title)


async def test_page_targeting_requires_a_configured_prefix() -> None:
    directory = make_directory(InMemoryProfiles(), page_prefix="")
    with pytest.raises(SelfServiceUnavailableError):
        await directory.set_log_page(CHAT, "anything")


async def test_writes_require_a_store() -> None:
    directory = make_directory(store=None)
    with pytest.raises(SelfServiceUnavailableError):
        await directory.set_consent(CHAT, ConsentMode.AUTHOR_ONLY)
    with pytest.raises(SelfServiceUnavailableError):
        await directory.reset(CHAT)
    with pytest.raises(SelfServiceUnavailableError):
        await directory.profile_of(CHAT)


async def test_storage_outage_degrades_reads_to_defaults() -> None:
    """A database outage must never stop /log from publishing."""
    store = InMemoryProfiles()
    directory = make_directory(store)
    await directory.set_log_page(CHAT, "Telegram logs/Ours")
    store.fail = True
    settings = await directory.resolve(CHAT)
    assert settings.log_page == "Next 25/Telegram logs"  # defaults, not a crash


async def test_storage_outage_surfaces_on_writes() -> None:
    store = InMemoryProfiles(fail=True)
    directory = make_directory(store)
    with pytest.raises(StorageError):
        await directory.set_consent(CHAT, ConsentMode.AUTHOR_ONLY)


async def test_reset_forgets_everything() -> None:
    store = InMemoryProfiles()
    directory = make_directory(store)
    await directory.set_log_page(CHAT, "Telegram logs/Ours")
    await store.store_token(CHAT, "ghp_x")
    await directory.reset(CHAT)
    assert await store.get(CHAT) is None
    assert await store.fetch_token(CHAT) is None


async def test_profile_of_returns_empty_profile_for_display() -> None:
    directory = make_directory(InMemoryProfiles())
    profile = await directory.profile_of(CHAT)
    assert profile == GroupProfile(chat_id=CHAT)


async def test_set_repo_stores_the_binding() -> None:
    directory = make_directory(InMemoryProfiles())
    await directory.set_repo(CHAT, "wikimedia/mediawiki")
    assert (await directory.resolve(CHAT)).repo == "wikimedia/mediawiki"
