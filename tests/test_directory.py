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
    page_suffix: str = "Telegram logs",
) -> ChannelDirectory:
    return ChannelDirectory(
        store=store,
        default_log_page="Next 25/Telegram logs",
        default_consent=ConsentMode.IMMEDIATE,
        default_repo="schiste/blybot",
        page_suffix=page_suffix,
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
    await directory.set_log_page(CHAT, "WikiProject Ours")
    settings = await directory.resolve(CHAT)
    assert settings.log_page == "WikiProject Ours/Telegram logs"
    assert settings.consent_mode is ConsentMode.IMMEDIATE  # unset field: default
    assert settings.customized


async def test_set_consent_is_per_group() -> None:
    directory = make_directory(InMemoryProfiles())
    await directory.set_consent(CHAT, ConsentMode.AUTHOR_ONLY)
    assert (await directory.resolve(CHAT)).consent_mode is ConsentMode.AUTHOR_ONLY
    assert (await directory.resolve(-2)).consent_mode is ConsentMode.IMMEDIATE


async def test_setpage_appends_the_suffix_and_normalizes() -> None:
    directory = make_directory(InMemoryProfiles())
    assert await directory.set_log_page(CHAT, "WikiProject_Med") == (
        "WikiProject Med/Telegram logs"
    )
    # Any base path is adaptable: userspace, project space, whatever fits.
    assert await directory.set_log_page(CHAT, "User:Foo") == "User:Foo/Telegram logs"


async def test_setpage_is_idempotent_when_the_suffix_is_already_present() -> None:
    directory = make_directory(InMemoryProfiles())
    page = await directory.set_log_page(CHAT, "Next 25/Telegram logs")
    assert page == "Next 25/Telegram logs"  # not doubled


@pytest.mark.parametrize(
    "title",
    [
        "",  # empty base
        "/leading",  # leading slash
        "trailing/",  # trailing slash
        "Bad {{title}}",  # forbidden characters
        "x" * 300,  # over MediaWiki's title limit once the suffix is added
    ],
)
async def test_invalid_base_paths_are_rejected(title: str) -> None:
    directory = make_directory(InMemoryProfiles())
    with pytest.raises(PageNotAllowedError):
        await directory.set_log_page(CHAT, title)


async def test_page_targeting_requires_a_configured_suffix() -> None:
    directory = make_directory(InMemoryProfiles(), page_suffix="")
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
    await directory.set_log_page(CHAT, "WikiProject Ours")
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


async def test_migrate_is_a_noop_without_a_store() -> None:
    directory = make_directory(store=None)
    await directory.migrate(-1, -2)  # v1 mode: nothing to carry, no raise


async def test_migrate_moves_the_profile() -> None:
    store = InMemoryProfiles()
    directory = make_directory(store)
    await directory.set_log_page(CHAT, "WikiProject Ours")
    await directory.migrate(CHAT, -777)
    assert (await directory.resolve(-777)).log_page == "WikiProject Ours/Telegram logs"
