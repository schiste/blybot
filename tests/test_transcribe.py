"""DmTranscriptionService tests (spec R4, N2, N3)."""

from __future__ import annotations

import asyncio
from datetime import timedelta

import pytest

from blybot.domain.ports import WikiWriteError
from blybot.services.sessions import SessionRegistry
from blybot.services.transcribe import DmTranscriptionService
from tests.fakes import FakeClock, FakePublisher, PassthroughSanitizer, SequentialPseudonyms

TTL = timedelta(minutes=45)


class FailingPublisher:
    async def append(self, page: str, text: str, summary: str) -> None:  # noqa: ARG002
        raise WikiWriteError


def make_service(
    publisher: FakePublisher | FailingPublisher,
    clock: FakeClock,
    debounce_seconds: float = 0.0,
) -> DmTranscriptionService:
    sessions = SessionRegistry(pseudonyms=SequentialPseudonyms(), clock=clock, ttl=TTL)
    return DmTranscriptionService(
        publisher=publisher,
        sanitizer=PassthroughSanitizer(),
        sessions=sessions,
        target_base="Meta:Community/Discussions",
        edit_summary="Log entry via Blybot",
        debounce_seconds=debounce_seconds,
    )


async def test_immediate_mode_appends_each_message_to_the_session_subpage() -> None:
    publisher = FakePublisher()
    service = make_service(publisher, FakeClock())
    session = await service.record(chat_id=1, text="hello")

    (page, text, summary) = publisher.appends[0]
    assert page == "Meta:Community/Discussions/Anon-1"
    assert text == "\n: Anon-1: [sanitized]hello"
    assert summary == "Log entry via Blybot"
    assert service.page_for(session) == page


async def test_sanitizer_runs_on_every_dm_line() -> None:
    publisher = FakePublisher()
    service = make_service(publisher, FakeClock())
    await service.record(chat_id=1, text="{{Delete}}")
    assert "[sanitized]{{Delete}}" in publisher.appends[0][1]


async def test_burst_is_coalesced_into_one_edit() -> None:
    publisher = FakePublisher()
    service = make_service(publisher, FakeClock(), debounce_seconds=0.05)
    await service.record(chat_id=1, text="one")
    await service.record(chat_id=1, text="two")
    assert publisher.appends == []  # still inside the window

    await asyncio.sleep(0.1)
    (_page, text, _) = publisher.appends[0]
    assert text == "\n: Anon-1: [sanitized]one\n: Anon-1: [sanitized]two"
    assert len(publisher.appends) == 1


async def test_messages_after_a_flush_start_a_new_burst() -> None:
    publisher = FakePublisher()
    service = make_service(publisher, FakeClock(), debounce_seconds=0.03)
    await service.record(chat_id=1, text="one")
    await asyncio.sleep(0.06)
    await service.record(chat_id=1, text="two")
    await asyncio.sleep(0.06)
    assert len(publisher.appends) == 2


async def test_concurrent_sessions_never_share_a_page() -> None:
    publisher = FakePublisher()
    service = make_service(publisher, FakeClock())
    await service.record(chat_id=1, text="from A")
    await service.record(chat_id=2, text="from B")
    pages = {page for (page, _, _) in publisher.appends}
    assert pages == {
        "Meta:Community/Discussions/Anon-1",
        "Meta:Community/Discussions/Anon-2",
    }


async def test_session_rollover_mid_buffer_splits_the_pages() -> None:
    """A TTL rollover between buffered messages must not mix identities."""
    publisher = FakePublisher()
    clock = FakeClock()
    service = make_service(publisher, clock, debounce_seconds=10)
    await service.record(chat_id=1, text="before")
    clock.advance(TTL)  # session expires inside the debounce window
    await service.record(chat_id=1, text="after")
    await service.flush_all()

    by_page = {page: text for (page, text, _) in publisher.appends}
    assert by_page["Meta:Community/Discussions/Anon-1"] == "\n: Anon-1: [sanitized]before"
    assert by_page["Meta:Community/Discussions/Anon-2"] == "\n: Anon-2: [sanitized]after"


async def test_flush_all_drains_pending_buffers() -> None:
    publisher = FakePublisher()
    service = make_service(publisher, FakeClock(), debounce_seconds=60)
    await service.record(chat_id=1, text="pending")
    assert publisher.appends == []
    await service.flush_all()
    assert len(publisher.appends) == 1
    await service.flush_all()  # idempotent
    assert len(publisher.appends) == 1


async def test_debounced_flush_failure_is_contained() -> None:
    """A failed background flush is logged, never raised into the handler."""
    service = make_service(FailingPublisher(), FakeClock(), debounce_seconds=0.02)
    await service.record(chat_id=1, text="doomed")
    await asyncio.sleep(0.05)  # the flusher task must swallow the error


async def test_immediate_flush_failure_propagates() -> None:
    service = make_service(FailingPublisher(), FakeClock())
    with pytest.raises(WikiWriteError):
        await service.record(chat_id=1, text="doomed")


def test_peek_reports_live_sessions_without_minting() -> None:
    clock = FakeClock()
    sessions = SessionRegistry(pseudonyms=SequentialPseudonyms(), clock=clock, ttl=TTL)
    assert sessions.peek(chat_id=1) is None
    minted = sessions.touch(chat_id=1)
    assert sessions.peek(chat_id=1) == minted
    clock.advance(TTL)
    assert sessions.peek(chat_id=1) is None
