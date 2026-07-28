"""CommandService: shared command logic proven independent of any adapter.

Drives :class:`CommandService.capture` / ``set_page`` directly with plain
:class:`Scope` arguments and an in-memory profile store — no Telegram or
Discord object in sight. Both adapters delegate here, so covering every
branch once is the "logic tested without an adapter" proof issue #32 asks
for.
"""

from __future__ import annotations

from datetime import timedelta

from blybot.domain.models import ConsentMode, LlmSettings, Scope
from blybot.observability import Counters
from blybot.services import commands as c
from blybot.services.capture import CaptureService
from blybot.services.commands import CommandService
from blybot.services.directory import ChannelDirectory
from blybot.services.policy import GroupPolicy, SlidingWindowLimiter
from blybot.services.rules import parse_rule
from tests.fakes import FakeClock, InMemoryArchive, InMemoryProfiles

_CHANNEL = 555000111
_SCOPE = Scope("neutral", str(_CHANNEL))
_TOPIC = Scope("neutral", str(_CHANNEL), "7")
_CEILING = 4096


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
    allowed: set[int] | None = None,
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
    service, _store, _capture = _service(allowed={999})
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
