"""CommandService: shared command logic proven independent of any adapter.

Drives :class:`CommandService.capture` / ``set_page`` directly with plain
:class:`Scope` arguments and an in-memory profile store — no Telegram or
Discord object in sight. Both adapters delegate here, so covering every
branch once is the "logic tested without an adapter" proof issue #32 asks
for.
"""

from __future__ import annotations

from datetime import timedelta

from blybot.domain.models import ConsentMode, Scope
from blybot.observability import Counters
from blybot.services import commands as c
from blybot.services.capture import CaptureService
from blybot.services.commands import CommandService
from blybot.services.directory import ChannelDirectory
from blybot.services.policy import GroupPolicy, SlidingWindowLimiter
from tests.fakes import FakeClock, InMemoryArchive, InMemoryProfiles

_CHANNEL = 555000111
_SCOPE = Scope("neutral", str(_CHANNEL))


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
) -> tuple[CommandService, InMemoryProfiles, CaptureService | None]:
    store = store if store is not None else InMemoryProfiles()
    capture = _capture_service(store, InMemoryArchive()) if with_capture else None
    service = CommandService(
        directory=_directory(store, page_suffix=page_suffix),
        groups=GroupPolicy(allowed=allowed if allowed is not None else set()),
        page_url_for=lambda title: f"https://wiki/{title.replace(' ', '_')}",
        counters=Counters(),
        capture_service=capture,
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
