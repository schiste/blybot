"""CommandService: shared command logic proven independent of any adapter.

Drives :class:`CommandService.capture` / ``set_page`` directly with plain
:class:`Scope` arguments and an in-memory profile store — no Telegram or
Discord object in sight. Both adapters delegate here, so covering every
branch once is the "logic tested without an adapter" proof issue #32 asks
for.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import timedelta
from typing import Any, cast

from blybot.adapters.telegram.transport import TELEGRAM_CAPABILITIES
from blybot.domain.models import (
    ConsentMode,
    LlmSettings,
    LogContent,
    OutboundMessage,
    Scope,
)
from blybot.domain.ports import WikiWriteError
from blybot.observability import Counters
from blybot.services import commands as c
from blybot.services.actions import MAX_ACTIONS, describe_action, parse_action
from blybot.services.capture import CaptureService
from blybot.services.commands import CommandService
from blybot.services.directory import ChannelDirectory
from blybot.services.engine import ActionEngine
from blybot.services.policy import GroupPolicy, SlidingWindowLimiter
from blybot.services.publish import NothingToPublishError
from blybot.services.repo import GroupRepoService
from blybot.services.rules import MAX_RULES, parse_rule
from tests.fakes import (
    FakeClock,
    FakeRepoGateway,
    FakeSink,
    InMemoryActions,
    InMemoryArchive,
    InMemoryProfiles,
    InMemorySubscriptions,
    SuffixTransform,
)

_CHANNEL = 555000111
_SCOPE = Scope("neutral", str(_CHANNEL))
_TOPIC = Scope("neutral", str(_CHANNEL), "7")
_CEILING = 4096
_LOG_CONFIRMATION = OutboundMessage(scope=_SCOPE, text="Published: https://wiki/Page")


class _RaisingTransform:
    """A log_publish stand-in that fails the way the real pipeline can."""

    def __init__(self, error: type[Exception]) -> None:
        self._error = error

    async def apply(self, _context: object, _step: object, _payload: object) -> object:
        raise self._error


def _directory(store: InMemoryProfiles | None, *, page_suffix: str = "Logs") -> ChannelDirectory:
    return ChannelDirectory(
        store=store,
        default_log_page="Project:Log",
        default_consent=ConsentMode.IMMEDIATE,
        default_repo="",
        page_suffix=page_suffix,
    )


def _capture_service(store: InMemoryProfiles, archive: InMemoryArchive) -> CaptureService:
    clock = FakeClock()
    return CaptureService(
        store=store,
        archive=archive,
        limiter=SlidingWindowLimiter(clock=clock, limit=1000, window=timedelta(minutes=1)),
        clock=clock,
        counters=Counters(),
        max_chars=2000,
    )


def _service(
    *,
    store: InMemoryProfiles | None = None,
    allowed: set[str] | None = None,
    page_suffix: str = "Logs",
    with_capture: bool = True,
    with_vault: bool = True,
    llm_defaults: LlmSettings | None = None,
) -> tuple[CommandService, InMemoryProfiles, CaptureService | None]:
    store = store if store is not None else InMemoryProfiles()
    capture = _capture_service(store, InMemoryArchive()) if with_capture else None
    service = CommandService(
        directory=_directory(store, page_suffix=page_suffix),
        groups=GroupPolicy(allowed=allowed if allowed is not None else set()),
        page_url_for=lambda title: f"https://wiki/{title.replace(' ', '_')}",
        counters=Counters(),
        capture_service=capture,
        vault=store if with_vault else None,
        llm_defaults=llm_defaults,
        llm_max_tokens_ceiling=_CEILING,
    )
    return service, store, capture


# --- capture -----------------------------------------------------------------


async def test_capture_rejects_non_admins() -> None:
    service, store, _capture = _service()
    result = await service.capture(_SCOPE, is_admin=False, enabled=True)
    assert result.text == c.REPLY_NOT_ADMIN
    assert _SCOPE not in store.profiles  # nothing written for a non-admin


async def test_capture_reports_when_capture_is_off_on_the_deployment() -> None:
    service, _store, _capture = _service(with_capture=False)
    result = await service.capture(_SCOPE, is_admin=True, enabled=True)
    assert result.text == c.REPLY_CAPTURE_OFF_DEPLOY


async def test_capture_refuses_a_scope_outside_the_allowlist() -> None:
    service, _store, _capture = _service(allowed={"999"})
    result = await service.capture(_SCOPE, is_admin=True, enabled=True)
    assert result.text == c.REPLY_NOT_ALLOWED


async def test_capture_enables_then_disables() -> None:
    service, store, _capture = _service()
    on = await service.capture(_SCOPE, is_admin=True, enabled=True)
    assert on.text == c.REPLY_CAPTURE_ENABLED
    assert store.profiles[_SCOPE].capture_enabled is True
    off = await service.capture(_SCOPE, is_admin=True, enabled=False)
    assert off.text == c.REPLY_CAPTURE_DISABLED
    assert store.profiles[_SCOPE].capture_enabled is False


async def test_capture_off_tombstones_on_a_failed_disable() -> None:
    service, _store, capture = _service(store=InMemoryProfiles(fail=True))
    assert capture is not None
    result = await service.capture(_SCOPE, is_admin=True, enabled=False)
    assert result.text == c.REPLY_STORAGE_DOWN
    assert _SCOPE in capture._denied  # fail-closed until the disable lands


async def test_capture_on_does_not_tombstone_a_failed_enable() -> None:
    service, _store, capture = _service(store=InMemoryProfiles(fail=True))
    assert capture is not None
    result = await service.capture(_SCOPE, is_admin=True, enabled=True)
    assert result.text == c.REPLY_STORAGE_DOWN
    assert _SCOPE not in capture._denied  # a failed enable already stays off


# --- set_page ----------------------------------------------------------------


async def test_set_page_rejects_non_admins() -> None:
    service, _store, _capture = _service()
    result = await service.set_page(_SCOPE, is_admin=False, page="P")
    assert result.text == c.REPLY_NOT_ADMIN


async def test_set_page_shows_usage_for_a_blank_page() -> None:
    service, _store, _capture = _service()
    result = await service.set_page(_SCOPE, is_admin=True, page="   ")
    assert result.text == c.REPLY_SETPAGE_USAGE.format(suffix="Logs")


async def test_set_page_stores_the_composed_page_and_links_it() -> None:
    service, store, _capture = _service()
    result = await service.set_page(_SCOPE, is_admin=True, page="WikiProject Foo")
    assert store.profiles[_SCOPE].log_page == "WikiProject Foo/Logs"
    assert result.text == c.REPLY_PAGE_SET.format(url="https://wiki/WikiProject_Foo/Logs")


async def test_set_page_refuses_an_invalid_page() -> None:
    service, _store, _capture = _service()
    result = await service.set_page(_SCOPE, is_admin=True, page="bad|title")
    assert result.text == c.REPLY_PAGE_REFUSED.format(suffix="Logs")


async def test_set_page_reports_when_self_service_is_off() -> None:
    service, _store, _capture = _service(page_suffix="")
    result = await service.set_page(_SCOPE, is_admin=True, page="WikiProject Foo")
    assert result.text == c.REPLY_SELF_SERVICE_OFF


async def test_set_page_reports_storage_down() -> None:
    service, _store, _capture = _service(store=InMemoryProfiles(fail=True))
    result = await service.set_page(_SCOPE, is_admin=True, page="WikiProject Foo")
    assert result.text == c.REPLY_STORAGE_DOWN


def _no_store_service() -> CommandService:
    """A service whose directory has no store — self-service is unavailable."""
    return CommandService(
        directory=_directory(None),
        groups=GroupPolicy(allowed=set()),
        page_url_for=str,
        counters=Counters(),
        capture_service=None,
    )


# --- show_settings -----------------------------------------------------------


async def test_settings_rejects_non_admins() -> None:
    service, _store, _capture = _service()
    result = await service.show_settings(_SCOPE, is_admin=False)
    assert result.text == c.REPLY_NOT_ADMIN
    assert result.ok is False


async def test_settings_reports_when_self_service_is_off() -> None:
    result = await _no_store_service().show_settings(_SCOPE, is_admin=True)
    assert result.text == c.REPLY_SELF_SERVICE_OFF
    assert result.ok is False


async def test_settings_reports_storage_down() -> None:
    service, _store, _capture = _service(store=InMemoryProfiles(fail=True))
    result = await service.show_settings(_SCOPE, is_admin=True)
    assert result.text == c.REPLY_STORAGE_DOWN


async def test_settings_shows_all_defaults_for_an_unconfigured_scope() -> None:
    service, _store, _capture = _service()
    result = await service.show_settings(_SCOPE, is_admin=True)
    assert result.ok is True
    assert "(all defaults)" in result.text
    assert "https://wiki/Project:Log" in result.text
    assert "repo: none" in result.text
    assert "repo token stored: no" in result.text
    assert "repo notifications: off" in result.text
    assert "message capture: off" in result.text
    assert "digest subscriptions: off" in result.text
    assert "LLM settings: deployment defaults" in result.text


async def test_settings_reports_a_customized_scope() -> None:
    service, store, _capture = _service()
    await service.directory.set_log_page(_SCOPE, "WikiProject Foo")
    # The token is reported only alongside the repo it is bound to (resolve
    # ties has_token to the binding scope), so bind a repo first.
    await service.directory.set_repo(_SCOPE, "owner/repo")
    await store.store_token(_SCOPE, "ghp_x")
    await service.directory.add_rule(_SCOPE, parse_rule("pr.merged digest"))
    await service.directory.set_events(_SCOPE, enabled=True)
    await service.directory.set_capture(_SCOPE, enabled=True)
    await service.directory.set_subscribe_code(_SCOPE, "code123")
    await service.directory.set_llm(_SCOPE, LlmSettings(model="large", lang="fr"))
    result = await service.show_settings(_SCOPE, is_admin=True)
    assert "(all defaults)" not in result.text
    assert "https://wiki/WikiProject_Foo/Logs" in result.text
    assert "repo: owner/repo" in result.text
    assert "repo token stored: yes" in result.text
    assert "repo notifications: on (1 rule(s))" in result.text
    assert "message capture: on" in result.text
    assert "digest subscriptions: on" in result.text
    assert "LLM settings: platform:liftwing model:large lang:fr" in result.text


# --- reset -------------------------------------------------------------------


async def test_reset_rejects_non_admins() -> None:
    service, _store, _capture = _service()
    result = await service.reset(_SCOPE, is_admin=False)
    assert result.text == c.REPLY_NOT_ADMIN
    assert result.ok is False


async def test_reset_forgets_the_profile() -> None:
    service, store, _capture = _service()
    await service.directory.set_log_page(_SCOPE, "WikiProject Foo")
    result = await service.reset(_SCOPE, is_admin=True)
    assert result.text == c.REPLY_RESET
    assert result.ok is True
    assert store.profiles == {}


async def test_reset_reports_when_self_service_is_off() -> None:
    result = await _no_store_service().reset(_SCOPE, is_admin=True)
    assert result.text == c.REPLY_SELF_SERVICE_OFF


async def test_reset_reports_storage_down() -> None:
    service, _store, _capture = _service(store=InMemoryProfiles(fail=True))
    result = await service.reset(_SCOPE, is_admin=True)
    assert result.text == c.REPLY_STORAGE_DOWN


# --- revoke_token ------------------------------------------------------------


async def test_revoke_rejects_non_admins() -> None:
    service, _store, _capture = _service()
    result = await service.revoke_token(_SCOPE, is_admin=False)
    assert result.text == c.REPLY_NOT_ADMIN
    assert result.ok is False


async def test_revoke_without_a_vault_reports_self_service_off() -> None:
    service, _store, _capture = _service(with_vault=False)
    result = await service.revoke_token(_SCOPE, is_admin=True)
    assert result.text == c.REPLY_SELF_SERVICE_OFF
    assert result.ok is False


async def test_revoke_discards_the_stored_token() -> None:
    service, store, _capture = _service()
    await store.store_token(_SCOPE, "ghp_x")
    result = await service.revoke_token(_SCOPE, is_admin=True)
    assert result.text == c.REPLY_REVOKED
    assert result.ok is True
    assert store.tokens == {}


async def test_revoke_reports_storage_down() -> None:
    service, _store, _capture = _service(store=InMemoryProfiles(fail=True))
    result = await service.revoke_token(_SCOPE, is_admin=True)
    assert result.text == c.REPLY_STORAGE_DOWN


# --- set_llm -----------------------------------------------------------------


def _llm_service(
    store: InMemoryProfiles | None = None,
) -> tuple[CommandService, InMemoryProfiles]:
    service, store, _capture = _service(store=store, llm_defaults=LlmSettings())
    return service, store


async def test_llm_rejects_non_admins() -> None:
    service, _store = _llm_service()
    result = await service.set_llm(_SCOPE, is_admin=False, tokens=["show"])
    assert result.text == c.REPLY_NOT_ADMIN
    assert result.ok is False


async def test_llm_reports_when_off_on_the_deployment() -> None:
    service, _store, _capture = _service()  # llm_defaults left None
    result = await service.set_llm(_SCOPE, is_admin=True, tokens=["show"])
    assert result.text == c.REPLY_LLM_OFF_DEPLOY
    assert result.ok is False


async def test_llm_shows_usage_for_bad_subcommands() -> None:
    service, _store = _llm_service()
    for tokens in ([], ["set"], ["frobnicate"]):
        result = await service.set_llm(_SCOPE, is_admin=True, tokens=tokens)
        assert result.text == c.REPLY_LLM_USAGE
        assert result.ok is False


async def test_llm_show_reports_deployment_defaults_then_scope_override() -> None:
    service, store = _llm_service()
    default = await service.set_llm(_SCOPE, is_admin=True, tokens=["show"])
    assert c._LLM_ORIGIN_DEFAULT in default.text
    assert "model:default" in default.text

    updated = await service.set_llm(_SCOPE, is_admin=True, tokens=["set", "model:large", "lang:fr"])
    assert updated.text == c.REPLY_LLM_SET.format(
        line="platform:liftwing model:large lang:fr temp:0.2 max_tokens:1024"
    )
    assert store.profiles[_SCOPE].llm == LlmSettings(model="large", lang="fr")

    own = await service.set_llm(_SCOPE, is_admin=True, tokens=["show"])
    assert c._LLM_ORIGIN_OWN in own.text


async def test_llm_in_a_thread_builds_on_the_inherited_parent_settings() -> None:
    service, store = _llm_service()
    await service.set_llm(_SCOPE, is_admin=True, tokens=["set", "model:large", "lang:fr"])

    shown = await service.set_llm(_TOPIC, is_admin=True, tokens=["show"])
    assert c._LLM_ORIGIN_INHERITED in shown.text
    assert "model:large" in shown.text

    # A partial edit in the thread keeps the inherited model/lang.
    await service.set_llm(_TOPIC, is_admin=True, tokens=["set", "temp:0.4"])
    assert store.profiles[_TOPIC].llm == LlmSettings(model="large", lang="fr", temperature=0.4)


async def test_llm_thread_without_parent_settings_shows_deployment_defaults() -> None:
    service, _store = _llm_service()
    shown = await service.set_llm(_TOPIC, is_admin=True, tokens=["show"])
    assert c._LLM_ORIGIN_DEFAULT in shown.text


async def test_llm_set_rejects_bad_values_verbatim() -> None:
    service, _store = _llm_service()
    result = await service.set_llm(_SCOPE, is_admin=True, tokens=["set", "temp:2"])
    assert "between 0 and 1" in result.text
    assert result.ok is False


async def test_llm_reset_clears_the_scope_override() -> None:
    service, store = _llm_service()
    await service.set_llm(_SCOPE, is_admin=True, tokens=["set", "model:large"])
    result = await service.set_llm(_SCOPE, is_admin=True, tokens=["reset"])
    assert result.text == c.REPLY_LLM_RESET
    assert result.ok is True
    assert store.profiles[_SCOPE].llm is None


async def test_llm_reports_storage_down() -> None:
    service, _store = _llm_service(store=InMemoryProfiles(fail=True))
    result = await service.set_llm(_SCOPE, is_admin=True, tokens=["show"])
    assert result.text == c.REPLY_STORAGE_DOWN


# --- events / rule / rules (issue #40) ---------------------------------------


def _storeless() -> CommandService:
    """A deployment with no profile store: every write is self-service-off."""
    return CommandService(
        directory=_directory(None),
        groups=GroupPolicy(allowed=set()),
        page_url_for=str,
        counters=Counters(),
    )


async def test_events_and_rules_reject_non_admins() -> None:
    service, _store, _capture = _service()
    results = [
        await service.events(_SCOPE, is_admin=False, tokens=["on"]),
        await service.set_events(_SCOPE, is_admin=False, enabled=True),
        await service.rule(_SCOPE, is_admin=False, tokens=["clear"]),
        await service.add_rule(_SCOPE, is_admin=False, spec="release"),
        await service.remove_rule(_SCOPE, is_admin=False, rule_id="abcd"),
        await service.clear_rules(_SCOPE, is_admin=False),
        await service.list_rules(_SCOPE, is_admin=False),
    ]
    assert [r.text for r in results] == [c.REPLY_NOT_ADMIN] * len(results)
    assert not any(r.ok for r in results)


async def test_events_usage_covers_every_non_toggle_argument() -> None:
    service, _store, _capture = _service()
    for tokens in ([], ["sometimes"], ["releases,prs"]):
        result = await service.events(_SCOPE, is_admin=True, tokens=list(tokens))
        assert result.text == c.REPLY_EVENTS_USAGE
        assert result.ok is False


async def test_events_on_seeds_then_keeps_the_ruleset() -> None:
    service, store, _capture = _service()
    await service.directory.set_repo(_SCOPE, "org/repo")

    seeded = await service.events(_SCOPE, is_admin=True, tokens=["on"])
    assert seeded.text == c.REPLY_EVENTS_SEEDED
    assert {rule.trigger.token for rule in store.profiles[_SCOPE].rules} == {
        "pr.merged",
        "release",
    }

    again = await service.events(_SCOPE, is_admin=True, tokens=["ON"])
    assert again.text == c.REPLY_EVENTS_SET.format(state="on")

    off = await service.events(_SCOPE, is_admin=True, tokens=["off"])
    assert off.text == c.REPLY_EVENTS_SET.format(state="off")
    assert store.profiles[_SCOPE].events_enabled is False


async def test_events_on_needs_a_repo_bound_at_this_very_scope() -> None:
    service, store, _capture = _service()
    result = await service.set_events(_SCOPE, is_admin=True, enabled=True)
    assert result.text == c.REPLY_EVENTS_NEED_REPO
    assert result.ok is False
    assert _SCOPE not in store.profiles or not store.profiles[_SCOPE].events_enabled


async def test_rule_dispatches_add_remove_and_clear() -> None:
    service, store, _capture = _service()
    added = await service.rule(_SCOPE, is_admin=True, tokens=["add", "pr.merged", "base:main"])
    (rule,) = store.profiles[_SCOPE].rules
    assert added.text == c.REPLY_RULE_ADDED.format(
        desc=f"[{rule.rule_id}] pr.merged base:main → live"
    )

    listing = await service.list_rules(_SCOPE, is_admin=True)
    assert listing.text.startswith("Rules for this scope:")
    assert rule.rule_id in listing.text

    removed = await service.rule(_SCOPE, is_admin=True, tokens=["remove", rule.rule_id])
    assert removed.text == c.REPLY_RULE_REMOVED.format(id=rule.rule_id)
    assert store.profiles[_SCOPE].rules == ()

    await service.add_rule(_SCOPE, is_admin=True, spec="release")
    cleared = await service.rule(_SCOPE, is_admin=True, tokens=["CLEAR"])
    assert cleared.text == c.REPLY_RULES_CLEARED.format(count=1)


async def test_rule_usage_covers_every_bad_subcommand() -> None:
    service, _store, _capture = _service()
    for tokens in ([], ["frobnicate"], ["remove"], ["remove", "a", "b"]):
        result = await service.rule(_SCOPE, is_admin=True, tokens=list(tokens))
        assert result.text == c.REPLY_RULE_USAGE
        assert result.ok is False


async def test_rule_add_surfaces_the_parse_error_verbatim() -> None:
    service, _store, _capture = _service()
    result = await service.add_rule(_SCOPE, is_admin=True, spec="nope.nope")
    assert "Unknown event type" in result.text
    assert result.ok is False


async def test_rule_add_enforces_the_per_scope_cap() -> None:
    service, store, _capture = _service()
    for _ in range(MAX_RULES):
        await service.add_rule(_SCOPE, is_admin=True, spec="pr.merged")
    result = await service.add_rule(_SCOPE, is_admin=True, spec="release")
    assert result.text == c.REPLY_RULES_FULL.format(max=MAX_RULES)
    assert len(store.profiles[_SCOPE].rules) == MAX_RULES


async def test_rule_remove_and_list_report_the_empty_cases() -> None:
    service, _store, _capture = _service()
    unknown = await service.remove_rule(_SCOPE, is_admin=True, rule_id="nope")
    assert unknown.text == c.REPLY_RULE_UNKNOWN.format(id="nope")
    assert unknown.ok is False

    empty = await service.list_rules(_SCOPE, is_admin=True)
    assert empty.text == c.REPLY_RULES_NONE
    assert empty.ok is False


async def test_events_and_rules_report_a_storage_outage() -> None:
    service, _store, _capture = _service(store=InMemoryProfiles(fail=True))
    results = [
        await service.set_events(_SCOPE, is_admin=True, enabled=True),
        await service.add_rule(_SCOPE, is_admin=True, spec="release"),
        await service.remove_rule(_SCOPE, is_admin=True, rule_id="abcd"),
        await service.clear_rules(_SCOPE, is_admin=True),
        await service.list_rules(_SCOPE, is_admin=True),
    ]
    assert [r.text for r in results] == [c.REPLY_STORAGE_DOWN] * len(results)
    assert not any(r.ok for r in results)


async def test_events_and_rules_report_self_service_off() -> None:
    service = _storeless()
    results = [
        await service.set_events(_SCOPE, is_admin=True, enabled=True),
        await service.add_rule(_SCOPE, is_admin=True, spec="release"),
        await service.remove_rule(_SCOPE, is_admin=True, rule_id="abcd"),
        await service.clear_rules(_SCOPE, is_admin=True),
        await service.list_rules(_SCOPE, is_admin=True),
    ]
    assert [r.text for r in results] == [c.REPLY_SELF_SERVICE_OFF] * len(results)
    assert not any(r.ok for r in results)


# --- setrepo / token storage (issue #43, increment 2) -------------------------

# Fixture values, not secrets: FakeRepoGateway accepts _GOOD_PAT and nothing else.
_GOOD_PAT = "ghp_good"
_WRONG_PAT = "ghp_wrong"


def _repo_service(
    *, store: InMemoryProfiles | None = None, with_actions: bool = True
) -> tuple[CommandService, InMemoryProfiles, FakeRepoGateway]:
    store = store if store is not None else InMemoryProfiles()
    gateway = FakeRepoGateway(valid_tokens={_GOOD_PAT})
    service = CommandService(
        directory=_directory(store),
        groups=GroupPolicy(allowed=set()),
        page_url_for=str,
        counters=Counters(),
        vault=store,
        repo_actions=gateway if with_actions else None,
    )
    return service, store, gateway


async def test_setrepo_and_store_token_reject_non_admins() -> None:
    service, store, _gateway = _repo_service()
    bind = await service.set_repo(_SCOPE, is_admin=False, repo="org/repo")
    stored = await service.store_token(_SCOPE, is_admin=False, token=_GOOD_PAT)
    assert [bind.text, stored.text] == [c.REPLY_NOT_ADMIN] * 2
    assert store.profiles == {}
    assert store.tokens == {}


async def test_setrepo_binds_and_discards_any_previous_token() -> None:
    service, store, _gateway = _repo_service()
    await store.store_token(_SCOPE, "pat-for-the-old-repo")
    result = await service.set_repo(_SCOPE, is_admin=True, repo="  org/repo  ")
    assert result.text == c.REPLY_REPO_BOUND.format(repo="org/repo")
    assert store.profiles[_SCOPE].repo == "org/repo"
    assert store.tokens == {}  # a token consented for repo A never survives


async def test_setrepo_refuses_malformed_repositories() -> None:
    service, store, _gateway = _repo_service()
    for bad in ("", "not-a-repo", "a/b/c", "owner/", "owner/..", "../x"):
        result = await service.set_repo(_SCOPE, is_admin=True, repo=bad)
        assert result.text == c.REPLY_SETREPO_USAGE
        assert result.ok is False
    assert store.profiles == {}


async def test_setrepo_reports_both_failure_modes() -> None:
    service, _store, _gateway = _repo_service(store=InMemoryProfiles(fail=True))
    down = await service.set_repo(_SCOPE, is_admin=True, repo="org/repo")
    assert down.text == c.REPLY_STORAGE_DOWN

    storeless = CommandService(
        directory=_directory(None),
        groups=GroupPolicy(allowed=set()),
        page_url_for=str,
        counters=Counters(),
    )
    off = await storeless.set_repo(_SCOPE, is_admin=True, repo="org/repo")
    assert off.text == c.REPLY_SELF_SERVICE_OFF


async def test_store_token_validates_against_the_bound_repo_then_encrypts() -> None:
    service, store, _gateway = _repo_service()
    await service.set_repo(_SCOPE, is_admin=True, repo="org/repo")

    rejected = await service.store_token(_SCOPE, is_admin=True, token=_WRONG_PAT)
    assert rejected.text == c.REPLY_PAT_INVALID
    assert rejected.ok is False
    assert store.tokens == {}  # an unvalidated secret is never persisted

    saved = await service.store_token(_SCOPE, is_admin=True, token=f"  {_GOOD_PAT}  ")
    assert saved.text == c.REPLY_PAT_SAVED
    assert saved.ok is True
    assert store.tokens[_SCOPE] == _GOOD_PAT


async def test_store_token_needs_a_repo_a_secret_and_the_wiring() -> None:
    service, store, _gateway = _repo_service()
    unbound = await service.store_token(_SCOPE, is_admin=True, token=_GOOD_PAT)
    assert unbound.text == c.REPLY_PAT_NO_REPO

    await service.set_repo(_SCOPE, is_admin=True, repo="org/repo")
    for blank in ("", "   "):
        empty = await service.store_token(_SCOPE, is_admin=True, token=blank)
        assert empty.text == c.REPLY_PAT_MISSING

    off, _store, _gw = _repo_service(with_actions=False)
    assert (
        await off.store_token(_SCOPE, is_admin=True, token=_GOOD_PAT)
    ).text == c.REPLY_PAT_OFF_DEPLOY
    assert store.tokens == {}


async def test_store_token_reports_a_vault_outage() -> None:
    store = InMemoryProfiles()
    service, _store, _gateway = _repo_service(store=store)
    await service.set_repo(_SCOPE, is_admin=True, repo="org/repo")
    store.fail_token_writes = True
    result = await service.store_token(_SCOPE, is_admin=True, token=_GOOD_PAT)
    assert result.text == c.REPLY_PAT_STORE_FAILED
    assert result.ok is False


# --- issue / repo (issue #42) ------------------------------------------------


def _issue_service(
    *,
    allowed: set[str] | None = None,
    with_service: bool = True,
    limit: int = 10,
) -> tuple[CommandService, InMemoryProfiles, FakeRepoGateway]:
    store = InMemoryProfiles()
    gateway = FakeRepoGateway(valid_tokens={_GOOD_PAT})
    directory = _directory(store)
    groups = GroupPolicy(allowed=allowed if allowed is not None else set())
    service = CommandService(
        directory=directory,
        groups=groups,
        page_url_for=str,
        counters=Counters(),
        repo_service=(
            GroupRepoService(gateway=gateway, vault=store, directory=directory)
            if with_service
            else None
        ),
        repo_limiter=SlidingWindowLimiter(
            clock=FakeClock(), limit=limit, window=timedelta(minutes=1)
        ),
    )
    return service, store, gateway


async def _bind(service: CommandService, store: InMemoryProfiles) -> None:
    await service.directory.set_repo(_SCOPE, "org/repo")
    await store.store_token(_SCOPE, _GOOD_PAT)


async def test_issue_files_anonymously_and_repo_summarizes() -> None:
    service, store, gateway = _issue_service()
    await _bind(service, store)

    filed = await service.file_issue(_SCOPE, description="the button is broken")
    assert filed.ok is True
    (repo, token, _title, body) = gateway.issues[0]
    assert (repo, token) == ("org/repo", _GOOD_PAT)
    assert "No reporter identity is recorded" in body
    assert "Telegram" not in body  # the preamble is platform-neutral

    summary = await service.repo_summary(_SCOPE)
    assert summary.ok is True
    assert "org/repo" in summary.text


async def test_issue_needs_a_description_and_the_deployment_wiring() -> None:
    service, _store, _gateway = _issue_service()
    for blank in ("", "   "):
        assert (await service.file_issue(_SCOPE, description=blank)).text == c.REPLY_ISSUE_USAGE

    off, _store, _gw = _issue_service(with_service=False)
    assert (await off.file_issue(_SCOPE, description="x")).text == c.REPLY_ISSUE_DISABLED
    assert (await off.repo_summary(_SCOPE)).text == c.REPLY_ISSUE_DISABLED


async def test_issue_and_repo_refuse_channels_outside_the_allowlist() -> None:
    service, _store, _gateway = _issue_service(allowed={"999"})
    assert (await service.file_issue(_SCOPE, description="x")).text == c.REPLY_NOT_ALLOWED
    assert (await service.repo_summary(_SCOPE)).text == c.REPLY_NOT_ALLOWED


async def test_issue_and_repo_report_a_missing_binding_or_token() -> None:
    service, store, _gateway = _issue_service()
    unbound = await service.file_issue(_SCOPE, description="x")
    assert unbound.text == c.REPLY_ISSUE_UNBOUND
    assert (await service.repo_summary(_SCOPE)).text == c.REPLY_ISSUE_UNBOUND

    await service.directory.set_repo(_SCOPE, "org/repo")  # bound, token never supplied
    assert (await service.file_issue(_SCOPE, description="x")).text == c.REPLY_ISSUE_NO_PAT
    assert (await service.repo_summary(_SCOPE)).text == c.REPLY_ISSUE_NO_PAT
    assert store.tokens == {}


async def test_issue_and_repo_surface_a_github_refusal() -> None:
    service, store, gateway = _issue_service()
    await _bind(service, store)
    gateway.fail = True
    assert (await service.file_issue(_SCOPE, description="x")).text == c.REPLY_ISSUE_FAILED
    assert (await service.repo_summary(_SCOPE)).text == c.REPLY_ISSUE_FAILED


async def test_issue_and_repo_are_rate_capped_per_command() -> None:
    service, store, _gateway = _issue_service(limit=1)
    await _bind(service, store)
    assert (await service.file_issue(_SCOPE, description="first")).ok is True
    assert (await service.file_issue(_SCOPE, description="second")).text == c.REPLY_THROTTLED
    # /repo has its own bucket, so it is unaffected by /issue's cap.
    assert (await service.repo_summary(_SCOPE)).ok is True
    assert (await service.repo_summary(_SCOPE)).text == c.REPLY_THROTTLED


# --- scheduled analyses: /action (issue #43, increment 3) ---------------------


def _action_service(*, with_actions: bool = True) -> tuple[CommandService, InMemoryActions]:
    store = InMemoryProfiles()
    actions = InMemoryActions()
    service = CommandService(
        directory=_directory(store),
        groups=GroupPolicy(allowed=set()),
        page_url_for=str,
        counters=Counters(),
        actions=actions if with_actions else None,
        clock=FakeClock() if with_actions else None,
    )
    return service, actions


async def test_action_rejects_non_admins() -> None:
    service, actions = _action_service()
    results = [
        await service.action(_SCOPE, is_admin=False, tokens=["list"]),
        await service.add_action(_SCOPE, is_admin=False, spec="daily@06:00 summarize"),
        await service.remove_action(_SCOPE, is_admin=False, action_id="abcd"),
        await service.list_actions(_SCOPE, is_admin=False),
    ]
    assert [r.text for r in results] == [c.REPLY_NOT_ADMIN] * len(results)
    assert actions.actions == {}


async def test_action_reports_an_unscheduled_deployment() -> None:
    service, _actions = _action_service(with_actions=False)
    results = [
        await service.add_action(_SCOPE, is_admin=True, spec="daily@06:00 summarize"),
        await service.remove_action(_SCOPE, is_admin=True, action_id="abcd"),
        await service.list_actions(_SCOPE, is_admin=True),
    ]
    assert [r.text for r in results] == [c.REPLY_ACTIONS_OFF_DEPLOY] * len(results)


async def test_action_dispatches_add_remove_and_list() -> None:
    service, actions = _action_service()
    added = await service.action(_SCOPE, is_admin=True, tokens=["add", "daily@06:00", "summarize"])
    (spec,) = actions.actions[_SCOPE]
    assert added.text == c.REPLY_ACTION_ADDED.format(desc=describe_action(spec))
    assert spec.last_run == FakeClock().now()  # primed so it isn't instantly due

    listing = await service.action(_SCOPE, is_admin=True, tokens=["list"])
    assert listing.text.startswith("Scheduled actions for this scope:")
    assert spec.action_id in listing.text

    removed = await service.action(_SCOPE, is_admin=True, tokens=["remove", spec.action_id])
    assert removed.text == c.REPLY_ACTION_REMOVED.format(id=spec.action_id)
    assert actions.actions[_SCOPE] == ()


async def test_action_usage_covers_every_bad_subcommand() -> None:
    service, _actions = _action_service()
    for tokens in ([], ["add"], ["remove"], ["remove", "a", "b"], ["frobnicate"]):
        result = await service.action(_SCOPE, is_admin=True, tokens=list(tokens))
        assert result.text == c.REPLY_ACTION_USAGE
        assert result.ok is False


async def test_action_surfaces_parse_errors_and_the_cap() -> None:
    service, actions = _action_service()
    bad = await service.add_action(_SCOPE, is_admin=True, spec="hourly summarize")
    assert "Unknown schedule" in bad.text

    actions.actions[_SCOPE] = tuple(
        parse_action("daily@06:00 summarize") for _ in range(MAX_ACTIONS)
    )
    full = await service.add_action(_SCOPE, is_admin=True, spec="daily@07:00 summarize")
    assert full.text == c.REPLY_ACTIONS_FULL.format(max=MAX_ACTIONS)
    assert len(actions.actions[_SCOPE]) == MAX_ACTIONS


async def test_action_reports_the_empty_and_unknown_cases() -> None:
    service, _actions = _action_service()
    empty = await service.list_actions(_SCOPE, is_admin=True)
    assert empty.text == c.REPLY_ACTIONS_NONE
    assert empty.ok is False

    unknown = await service.remove_action(_SCOPE, is_admin=True, action_id="nope")
    assert unknown.text == c.REPLY_ACTION_UNKNOWN.format(id="nope")


async def test_action_reports_a_storage_outage() -> None:
    service, actions = _action_service()
    actions.fail = True
    results = [
        await service.add_action(_SCOPE, is_admin=True, spec="daily@06:00 summarize"),
        await service.remove_action(_SCOPE, is_admin=True, action_id="abcd"),
        await service.list_actions(_SCOPE, is_admin=True),
    ]
    assert [r.text for r in results] == [c.REPLY_STORAGE_DOWN] * len(results)


# --- anonymous /log (issue #44) -----------------------------------------------


def _log_service(
    *,
    allowed: set[str] | None = None,
    store: InMemoryProfiles | None = None,
    with_engine: bool = True,
    limit: int = 10,
    sink_messages: tuple[OutboundMessage, ...] = (_LOG_CONFIRMATION,),
    transform: object | None = None,
) -> tuple[CommandService, InMemoryProfiles]:
    store = store if store is not None else InMemoryProfiles()
    directory = _directory(store, page_suffix="Logs")
    engine = ActionEngine(
        sources={},
        transforms={"log_publish": cast("Any", transform or SuffixTransform())},
        sinks={"chat_confirm": FakeSink(messages=sink_messages)},
        counters=Counters(),
        clock=FakeClock(),
    )
    service = CommandService(
        directory=directory,
        groups=GroupPolicy(allowed=allowed if allowed is not None else set()),
        page_url_for=str,
        counters=Counters(),
        engine=engine if with_engine else None,
        repo_limiter=SlidingWindowLimiter(
            clock=FakeClock(), limit=limit, window=timedelta(minutes=1)
        ),
    )
    return service, store


async def _with_page(service: CommandService) -> None:
    """Self-service is on whenever a store exists, so a scope must pick its own
    page before /log will publish — that is the guard, not a test artifact."""
    await service.directory.set_log_page(_SCOPE, "WikiProject Foo")


async def test_log_publishes_and_returns_the_confirmation() -> None:
    service, store = _log_service()  # no self-service page gate
    await _with_page(service)
    result = await service.log_message(
        _SCOPE, is_author=True, content=LogContent(text="worth keeping")
    )
    assert result.ok is True
    assert result.text == _LOG_CONFIRMATION.text
    # The only stored state is the page the admin chose — nothing about
    # the requester or the message author.
    assert store.profiles[_SCOPE].log_page == "WikiProject Foo/Logs"


async def test_log_refuses_a_channel_outside_the_allowlist() -> None:
    service, _store = _log_service(allowed={"999"})
    result = await service.log_message(_SCOPE, is_author=True, content=LogContent(text="x"))
    assert result.text == c.REPLY_NOT_ALLOWED
    assert result.ok is False


async def test_log_fails_closed_when_settings_are_unknown() -> None:
    """A storage outage must not publish on defaults: the consent policy and
    target page are both unknown right then."""
    service, _store = _log_service(store=InMemoryProfiles(fail=True))
    result = await service.log_message(_SCOPE, is_author=True, content=LogContent(text="x"))
    assert result.text == c.REPLY_LOG_CONFIG_UNAVAILABLE
    assert result.ok is False


async def test_log_requires_a_self_service_scope_to_choose_its_page() -> None:
    """Never leak a self-service scope's logs onto the operator default."""
    service, _store = _log_service()  # page_suffix set => self-service on
    result = await service.log_message(_SCOPE, is_author=True, content=LogContent(text="x"))
    assert result.text == c.REPLY_LOG_NO_PAGE
    assert result.ok is False


async def test_log_honours_the_author_only_consent_policy() -> None:
    store = InMemoryProfiles()
    service, _store = _log_service(store=store)
    await _with_page(service)
    await service.directory.set_consent(_SCOPE, ConsentMode.AUTHOR_ONLY)

    refused = await service.log_message(_SCOPE, is_author=False, content=LogContent(text="x"))
    assert refused.text == c.REPLY_LOG_AUTHOR_ONLY
    assert refused.ok is False

    allowed = await service.log_message(_SCOPE, is_author=True, content=LogContent(text="x"))
    assert allowed.ok is True


async def test_log_is_rate_capped() -> None:
    service, _store = _log_service(limit=1)
    await _with_page(service)
    assert (await service.log_message(_SCOPE, is_author=True, content=LogContent(text="a"))).ok
    second = await service.log_message(_SCOPE, is_author=True, content=LogContent(text="b"))
    assert second.text == c.REPLY_THROTTLED


async def test_log_reports_an_unpublishable_target_and_a_wiki_failure() -> None:
    nothing, _store = _log_service(transform=_RaisingTransform(NothingToPublishError))
    await _with_page(nothing)
    empty = await nothing.log_message(_SCOPE, is_author=True, content=LogContent())
    assert empty.text == c.REPLY_LOG_NOTHING
    assert empty.ok is False

    broken, _store2 = _log_service(transform=_RaisingTransform(WikiWriteError))
    await _with_page(broken)
    failed = await broken.log_message(_SCOPE, is_author=True, content=LogContent(text="x"))
    assert failed.text == c.REPLY_LOG_WIKI_ERROR
    assert failed.ok is False


async def test_log_without_an_engine_fails_closed() -> None:
    service, _store = _log_service(with_engine=False)
    result = await service.log_message(_SCOPE, is_author=True, content=LogContent(text="x"))
    assert result.text == c.REPLY_LOG_OFF_DEPLOY
    assert result.ok is False


async def test_log_with_a_silent_sink_reports_nothing_published() -> None:
    service, _store = _log_service(sink_messages=())
    await _with_page(service)
    result = await service.log_message(_SCOPE, is_author=True, content=LogContent(text="x"))
    assert result.text == c.REPLY_LOG_NOTHING
    assert result.ok is False


async def test_subscribe_needs_a_platform_with_durable_dms() -> None:
    """IRC has no durable DM, so there is nowhere to deliver a digest —
    the gate belongs in the neutral service, not in each adapter (#32)."""
    subs = InMemorySubscriptions()
    service = CommandService(
        directory=_directory(InMemoryProfiles()),
        groups=GroupPolicy(allowed=set()),
        page_url_for=str,
        counters=Counters(),
        subscriptions=subs,
        capabilities=replace(TELEGRAM_CAPABILITIES, durable_dm=False),
    )
    result = await service.subscribe(_SCOPE, Scope("neutral", "777"), options="")
    assert result.text == c.REPLY_SUBS_NO_DURABLE_DM
    assert result.ok is False
    assert subs.subs == {}


async def test_the_subscription_surface_is_off_without_a_store() -> None:
    """A deployment with no subscription store fails closed on all three."""
    service = CommandService(
        directory=_directory(InMemoryProfiles()),
        groups=GroupPolicy(allowed=set()),
        page_url_for=str,
        counters=Counters(),
    )
    dm = Scope("neutral", "777")
    results = [
        await service.subscribe(_SCOPE, dm, options=""),
        await service.list_subscriptions(dm),
        await service.unsubscribe(dm, subscription_id="abcd"),
    ]
    assert [r.text for r in results] == [c.REPLY_SUBS_UNAVAILABLE] * 3
    assert not any(r.ok for r in results)
