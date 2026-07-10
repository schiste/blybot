"""DmTranscriptionService tests (spec R4, N2, N3): one section per session."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest

from blybot.domain.models import Pseudonym, Session, TimestampGranularity
from blybot.domain.ports import WikiWriteError
from blybot.services.sessions import SessionRegistry
from blybot.services.transcribe import DmTranscriptionService
from tests.fakes import (
    FailingPublisher,
    FakeClock,
    FakePublisher,
    FlakyPublisher,
    PassthroughSanitizer,
    SequentialPseudonyms,
)

TTL = timedelta(minutes=45)
PAGE = "Meta talk:Community/Discussions"


def make_service(
    publisher: FakePublisher | FailingPublisher,
    clock: FakeClock,
    debounce_seconds: float = 0.0,
    granularity: TimestampGranularity = TimestampGranularity.NONE,
) -> DmTranscriptionService:
    sessions = SessionRegistry(pseudonyms=SequentialPseudonyms(), clock=clock, ttl=TTL)
    return DmTranscriptionService(
        publisher=publisher,
        sanitizer=PassthroughSanitizer(),
        sessions=sessions,
        target_page=PAGE,
        edit_summary="Log entry via Blybot",
        debounce_seconds=debounce_seconds,
        timestamp_granularity=granularity,
    )


async def test_messages_land_in_the_session_section_with_growing_indentation() -> None:
    publisher = FakePublisher()
    service = make_service(publisher, FakeClock())
    session = await service.record(chat_id=1, text="hello")
    await service.record(chat_id=1, text="and then")

    # The first flush OPENS the session's section; later ones continue it.
    assert publisher.started == [
        (PAGE, "Anon-1", ": [sanitized]hello --Anon-1", "Log entry via Blybot")
    ]
    (continuation,) = publisher.continued
    assert continuation[1] == "Anon-1"
    assert continuation[2] == ":: [sanitized]and then --Anon-1"  # one level deeper each time
    assert service.page_for(session) == f"{PAGE}#Anon-1"


async def test_sanitizer_runs_on_every_dm_line() -> None:
    publisher = FakePublisher()
    service = make_service(publisher, FakeClock())
    await service.record(chat_id=1, text="{{Delete}}")
    assert "[sanitized]{{Delete}}" in publisher.started[0][2]


async def test_burst_is_coalesced_into_one_edit_with_stepped_indentation() -> None:
    publisher = FakePublisher()
    service = make_service(publisher, FakeClock(), debounce_seconds=0.05)
    await service.record(chat_id=1, text="one")
    await service.record(chat_id=1, text="two")
    assert publisher.started == []  # still inside the window

    await asyncio.sleep(0.1)
    (_, heading, text, _) = publisher.started[0]
    assert heading == "Anon-1"
    assert text == ": [sanitized]one --Anon-1\n:: [sanitized]two --Anon-1"
    assert (len(publisher.started), len(publisher.continued)) == (1, 0)


async def test_messages_after_a_flush_continue_the_same_discussion_deeper() -> None:
    publisher = FakePublisher()
    service = make_service(publisher, FakeClock(), debounce_seconds=0.03)
    await service.record(chat_id=1, text="one")
    await asyncio.sleep(0.06)
    await service.record(chat_id=1, text="two")
    await asyncio.sleep(0.06)

    assert [entry[1] for entry in publisher.started] == ["Anon-1"]
    assert [entry[1] for entry in publisher.continued] == ["Anon-1"]
    assert publisher.continued[0][2] == ":: [sanitized]two --Anon-1"


async def test_concurrent_sessions_never_share_a_section() -> None:
    publisher = FakePublisher()
    service = make_service(publisher, FakeClock())
    await service.record(chat_id=1, text="from A")
    await service.record(chat_id=2, text="from B")
    assert {entry[1] for entry in publisher.started} == {"Anon-1", "Anon-2"}


async def test_session_rollover_mid_buffer_splits_the_sections() -> None:
    """A TTL rollover between buffered messages must not mix identities."""
    publisher = FakePublisher()
    clock = FakeClock()
    service = make_service(publisher, clock, debounce_seconds=10)
    await service.record(chat_id=1, text="before")
    clock.advance(TTL)  # session expires inside the debounce window
    await service.record(chat_id=1, text="after")
    await service.flush_all()

    by_heading = {heading: text for (_, heading, text, _) in publisher.started}
    assert by_heading["Anon-1"] == ": [sanitized]before --Anon-1"
    assert by_heading["Anon-2"] == ": [sanitized]after --Anon-2"  # fresh identity, depth resets
    assert publisher.continued == []  # both were first flushes of their sections


async def test_flush_all_drains_pending_buffers() -> None:
    publisher = FakePublisher()
    service = make_service(publisher, FakeClock(), debounce_seconds=60)
    await service.record(chat_id=1, text="pending")
    assert publisher.started == []
    await service.flush_all()
    assert len(publisher.started) == 1
    await service.flush_all()  # idempotent
    assert len(publisher.started) == 1


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


async def test_rollover_flush_failure_never_swallows_the_new_message() -> None:
    """A failed rollover flush follows the drop-and-log policy (old burst
    lost) but the message that triggered the rollover must still be
    buffered and published under the new identity."""
    clock = FakeClock()
    publisher = FlakyPublisher(failures=1)
    sessions = SessionRegistry(pseudonyms=SequentialPseudonyms(), clock=clock, ttl=TTL)
    service = DmTranscriptionService(
        publisher=publisher,
        sanitizer=PassthroughSanitizer(),
        sessions=sessions,
        target_page=PAGE,
        edit_summary="s",
        debounce_seconds=60,
        timestamp_granularity=TimestampGranularity.NONE,
    )
    await service.record(chat_id=1, text="before")
    clock.advance(TTL)
    await service.record(chat_id=1, text="after")  # rollover flush fails; must not raise
    await service.flush_all()

    assert [entry[1] for entry in publisher.recorder.started] == ["Anon-2"]
    assert publisher.recorder.started[0][2] == ": [sanitized]after --Anon-2"


async def test_flush_all_contains_write_failures() -> None:
    service = make_service(FailingPublisher(), FakeClock(), debounce_seconds=60)
    await service.record(chat_id=1, text="pending")
    await service.flush_all()  # failure is logged, never raised at shutdown


async def test_flush_of_an_unknown_chat_is_a_quiet_noop() -> None:
    """Guards the race where a scheduled flusher fires after its buffer
    was already drained by flush_all or a rollover."""
    publisher = FakePublisher()
    service = make_service(publisher, FakeClock())
    await service._flush(chat_id=999)
    assert publisher.wrote_nothing


def test_page_for_converts_spaces_to_wiki_anchor_underscores() -> None:
    service = make_service(FakePublisher(), FakeClock())
    session = Session(
        pseudonym=Pseudonym("Trillian Baggins from Gallifrey"),
        anchor="Trillian Baggins from Gallifrey",
        last_seen=datetime(2026, 7, 10, tzinfo=UTC),
        created_at=datetime(2026, 7, 10, tzinfo=UTC),
    )
    assert service.page_for(session) == f"{PAGE}#Trillian_Baggins_from_Gallifrey"


async def test_minute_granularity_headings_carry_session_start_time() -> None:
    """Heading = 'Date - HH:MM UTC : Pseudonym', stable across all bursts."""
    clock = FakeClock()
    publisher = FakePublisher()
    service = make_service(publisher, clock, granularity=TimestampGranularity.MINUTE)
    session = await service.record(chat_id=1, text="first")
    clock.advance(timedelta(minutes=7))  # later burst, same session
    await service.record(chat_id=1, text="second")

    headings = [publisher.started[0][1], publisher.continued[0][1]]
    assert headings == ["2026-07-10 - 12:00 UTC : Anon-1"] * 2  # start time, not send time
    assert service.page_for(session) == f"{PAGE}#2026-07-10_-_12:00_UTC_:_Anon-1"
