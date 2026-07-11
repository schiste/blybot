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
from blybot.services.rules import parse_rule
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
    await directory.set_log_page(CHAT, 0, "WikiProject Ours")
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
    assert await directory.set_log_page(CHAT, 0, "WikiProject_Med") == (
        "WikiProject Med/Telegram logs"
    )
    # Any base path is adaptable: userspace, project space, whatever fits.
    assert await directory.set_log_page(CHAT, 0, "User:Foo") == "User:Foo/Telegram logs"


async def test_setpage_is_idempotent_when_the_suffix_is_already_present() -> None:
    directory = make_directory(InMemoryProfiles())
    page = await directory.set_log_page(CHAT, 0, "Next 25/Telegram logs")
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
        await directory.set_log_page(CHAT, 0, title)


async def test_page_targeting_requires_a_configured_suffix() -> None:
    directory = make_directory(InMemoryProfiles(), page_suffix="")
    with pytest.raises(SelfServiceUnavailableError):
        await directory.set_log_page(CHAT, 0, "anything")


async def test_writes_require_a_store() -> None:
    directory = make_directory(store=None)
    with pytest.raises(SelfServiceUnavailableError):
        await directory.set_consent(CHAT, ConsentMode.AUTHOR_ONLY)
    with pytest.raises(SelfServiceUnavailableError):
        await directory.reset(CHAT, 0)
    with pytest.raises(SelfServiceUnavailableError):
        await directory.profile_of(CHAT, 0)


async def test_rule_ops_require_a_store() -> None:
    directory = make_directory(store=None)
    for op in (
        directory.add_rule(CHAT, 0, parse_rule("pr.merged")),
        directory.remove_rule(CHAT, 0, "x"),
        directory.clear_rules(CHAT, 0),
        directory.list_rules(CHAT, 0),
    ):
        with pytest.raises(SelfServiceUnavailableError):
            await op


async def test_remove_and_clear_on_an_empty_scope_are_no_ops() -> None:
    directory = make_directory(InMemoryProfiles())
    assert await directory.remove_rule(CHAT, 0, "x") is False  # no profile row at all
    assert await directory.clear_rules(CHAT, 0) == 0
    # a profile with no rules: remove misses, clear removes nothing
    await directory.set_repo(CHAT, 0, "org/repo")
    assert await directory.remove_rule(CHAT, 0, "x") is False
    assert await directory.clear_rules(CHAT, 0) == 0


async def test_enable_events_seeds_atomically_and_does_not_reseed() -> None:
    store = InMemoryProfiles()
    directory = make_directory(store)
    await directory.set_repo(CHAT, 0, "org/repo")
    seed = (parse_rule("pr.merged digest"), parse_rule("release digest"))

    assert await directory.enable_events(CHAT, 0, seed) is True  # seeded
    profile = store.profiles[CHAT, 0]
    assert profile.events_enabled
    assert profile.rules == seed

    # Enabling again keeps the (possibly edited) rules — never reseeds.
    assert await directory.enable_events(CHAT, 0, seed) is False
    assert store.profiles[CHAT, 0].rules == seed


async def test_add_rule_appends_and_preserves_other_fields() -> None:
    store = InMemoryProfiles()
    directory = make_directory(store)
    await directory.set_repo(CHAT, 0, "org/repo")
    await directory.add_rule(CHAT, 0, parse_rule("pr.merged"))
    await directory.add_rule(CHAT, 0, parse_rule("release"))
    profile = store.profiles[CHAT, 0]
    assert [rule.trigger.token for rule in profile.rules] == ["pr.merged", "release"]
    assert profile.repo == "org/repo"  # unrelated fields untouched


async def test_storage_outage_degrades_reads_to_defaults() -> None:
    """A database outage must never stop /log from publishing."""
    store = InMemoryProfiles()
    directory = make_directory(store)
    await directory.set_log_page(CHAT, 0, "Telegram logs/Ours")
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
    await directory.set_log_page(CHAT, 0, "WikiProject Ours")
    await store.store_token(CHAT, 0, "ghp_x")
    await directory.reset(CHAT, 0)
    assert await store.get(CHAT, 0) is None
    assert await store.fetch_token(CHAT, 0) is None


async def test_profile_of_returns_empty_profile_for_display() -> None:
    directory = make_directory(InMemoryProfiles())
    profile = await directory.profile_of(CHAT, 0)
    assert profile == GroupProfile(chat_id=CHAT, thread_id=0)


async def test_set_repo_stores_the_binding() -> None:
    directory = make_directory(InMemoryProfiles())
    await directory.set_repo(CHAT, 0, "wikimedia/mediawiki")
    assert (await directory.resolve(CHAT)).repo == "wikimedia/mediawiki"


async def test_migrate_is_a_noop_without_a_store() -> None:
    directory = make_directory(store=None)
    await directory.migrate(-1, -2)  # v1 mode: nothing to carry, no raise


async def test_migrate_moves_the_profile() -> None:
    store = InMemoryProfiles()
    directory = make_directory(store)
    await directory.set_log_page(CHAT, 0, "WikiProject Ours")
    await directory.migrate(CHAT, -777)
    assert (await directory.resolve(-777)).log_page == "WikiProject Ours/Telegram logs"


TOPIC = 42


async def test_topic_overrides_group_page_but_inherits_the_rest() -> None:
    store = InMemoryProfiles()
    directory = make_directory(store)
    await directory.set_log_page(CHAT, 0, "WikiProject Foo")  # group default
    await directory.set_consent(CHAT, ConsentMode.AUTHOR_ONLY)  # group-wide
    await directory.set_log_page(CHAT, TOPIC, "WikiProject Foo/Bugs")  # topic only

    topic = await directory.resolve(CHAT, TOPIC)
    assert topic.log_page == "WikiProject Foo/Bugs/Telegram logs"
    assert topic.consent_mode is ConsentMode.AUTHOR_ONLY  # inherited from group
    other = await directory.resolve(CHAT, 99)  # a topic with no profile
    assert other.log_page == "WikiProject Foo/Telegram logs"  # group default


async def test_topic_repo_and_token_resolve_as_a_unit() -> None:
    store = InMemoryProfiles()
    directory = make_directory(store)
    await directory.set_repo(CHAT, 0, "org/main")  # group repo
    await store.store_token(CHAT, 0, "group-token")
    await directory.set_repo(CHAT, TOPIC, "org/frontend")  # topic repo
    await store.store_token(CHAT, TOPIC, "topic-token")

    topic = await directory.resolve(CHAT, TOPIC)
    assert topic.repo == "org/frontend"
    assert topic.repo_thread_id == TOPIC  # token fetched from the topic
    inheriting = await directory.resolve(CHAT, 99)
    assert inheriting.repo == "org/main"
    assert inheriting.repo_thread_id == 0  # inherits the group's token


async def test_consent_is_group_wide_even_from_a_topic() -> None:
    store = InMemoryProfiles()
    directory = make_directory(store)
    await directory.set_consent(CHAT, ConsentMode.AUTHOR_ONLY)  # always thread 0
    assert store.profiles[CHAT, 0].consent_mode is ConsentMode.AUTHOR_ONLY
    assert (await directory.resolve(CHAT, TOPIC)).consent_mode is ConsentMode.AUTHOR_ONLY


async def test_resetting_a_topic_leaves_the_group_default() -> None:
    store = InMemoryProfiles()
    directory = make_directory(store)
    await directory.set_log_page(CHAT, 0, "WikiProject Foo")
    await directory.set_log_page(CHAT, TOPIC, "WikiProject Foo/Bugs")
    await directory.reset(CHAT, TOPIC)
    assert (await directory.resolve(CHAT, TOPIC)).log_page == "WikiProject Foo/Telegram logs"


async def test_page_explicit_flag_tracks_configuration() -> None:
    store = InMemoryProfiles()
    directory = make_directory(store)
    assert not (await directory.resolve(CHAT)).page_explicit  # nothing set
    await directory.set_log_page(CHAT, 0, "WikiProject Foo")
    assert (await directory.resolve(CHAT)).page_explicit  # group default set
    # A topic with its own page is explicit; a sibling topic inherits and
    # is still explicit because the group default is set.
    assert (await directory.resolve(CHAT, 5)).page_explicit


async def test_page_not_explicit_when_only_non_page_fields_set() -> None:
    store = InMemoryProfiles()
    directory = make_directory(store)
    await directory.set_consent(CHAT, ConsentMode.AUTHOR_ONLY)  # no page
    assert not (await directory.resolve(CHAT)).page_explicit
