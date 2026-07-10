"""DmTranscriptionService tests (spec R4, N2, N3): one section per session."""

from __future__ import annotations

import asyncio
from datetime import timedelta

import pytest

from blybot.domain.ports import WikiWriteError
from blybot.services.sessions import SessionRegistry
from blybot.services.transcribe import DmTranscriptionService
from tests.fakes import (
    FailingPublisher,
    FakeClock,
    FakePublisher,
    PassthroughSanitizer,
    SequentialPseudonyms,
)

TTL = timedelta(minutes=45)
PAGE = "Meta talk:Community/Discussions"


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
        target_page=PAGE,
        edit_summary="Log entry via Blybot",
        debounce_seconds=debounce_seconds,
    )


async def test_messages_land_in_the_session_section_with_growing_indentation() -> None:
    publisher = FakePublisher()
    service = make_service(publisher, FakeClock())
    session = await service.record(chat_id=1, text="hello")
    await service.record(chat_id=1, text="and then")

    first, second = publisher.continued
    assert first == (PAGE, "Anon-1", ": [sanitized]hello", "Log entry via Blybot")
    assert second[2] == ":: [sanitized]and then"  # one level deeper each message
    assert service.page_for(session) == f"{PAGE}#Anon-1"


async def test_sanitizer_runs_on_every_dm_line() -> None:
    publisher = FakePublisher()
    service = make_service(publisher, FakeClock())
    await service.record(chat_id=1, text="{{Delete}}")
    assert "[sanitized]{{Delete}}" in publisher.continued[0][2]


async def test_burst_is_coalesced_into_one_edit_with_stepped_indentation() -> None:
    publisher = FakePublisher()
    service = make_service(publisher, FakeClock(), debounce_seconds=0.05)
    await service.record(chat_id=1, text="one")
    await service.record(chat_id=1, text="two")
    assert publisher.continued == []  # still inside the window

    await asyncio.sleep(0.1)
    (_, heading, text, _) = publisher.continued[0]
    assert heading == "Anon-1"
    assert text == ": [sanitized]one\n:: [sanitized]two"
    assert len(publisher.continued) == 1


async def test_messages_after_a_flush_continue_the_same_discussion_deeper() -> None:
    publisher = FakePublisher()
    service = make_service(publisher, FakeClock(), debounce_seconds=0.03)
    await service.record(chat_id=1, text="one")
    await asyncio.sleep(0.06)
    await service.record(chat_id=1, text="two")
    await asyncio.sleep(0.06)

    assert [entry[1] for entry in publisher.continued] == ["Anon-1", "Anon-1"]
    assert publisher.continued[1][2] == ":: [sanitized]two"


async def test_concurrent_sessions_never_share_a_section() -> None:
    publisher = FakePublisher()
    service = make_service(publisher, FakeClock())
    await service.record(chat_id=1, text="from A")
    await service.record(chat_id=2, text="from B")
    assert {entry[1] for entry in publisher.continued} == {"Anon-1", "Anon-2"}


async def test_session_rollover_mid_buffer_splits_the_sections() -> None:
    """A TTL rollover between buffered messages must not mix identities."""
    publisher = FakePublisher()
    clock = FakeClock()
    service = make_service(publisher, clock, debounce_seconds=10)
    await service.record(chat_id=1, text="before")
    clock.advance(TTL)  # session expires inside the debounce window
    await service.record(chat_id=1, text="after")
    await service.flush_all()

    by_heading = {heading: text for (_, heading, text, _) in publisher.continued}
    assert by_heading["Anon-1"] == ": [sanitized]before"
    assert by_heading["Anon-2"] == ": [sanitized]after"  # fresh identity, depth resets


async def test_flush_all_drains_pending_buffers() -> None:
    publisher = FakePublisher()
    service = make_service(publisher, FakeClock(), debounce_seconds=60)
    await service.record(chat_id=1, text="pending")
    assert publisher.continued == []
    await service.flush_all()
    assert len(publisher.continued) == 1
    await service.flush_all()  # idempotent
    assert len(publisher.continued) == 1


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
