"""LogPublicationService tests (spec R2, R6, R8): one section per log."""

from __future__ import annotations

import pytest

from blybot.domain.models import TimestampGranularity
from blybot.services.publish import UNDATED_HEADING, LogPublicationService, NothingToPublishError
from tests.fakes import FakeClock, FakePublisher, PassthroughSanitizer


def make_service(
    publisher: FakePublisher,
    granularity: TimestampGranularity = TimestampGranularity.DATE,
) -> LogPublicationService:
    return LogPublicationService(
        publisher=publisher,
        sanitizer=PassthroughSanitizer(),
        clock=FakeClock(),
        target_page="Meta talk:Community/Log",
        edit_summary="Log entry via Blybot",
        timestamp_granularity=granularity,
    )


async def test_each_log_opens_its_own_section_with_an_indented_entry() -> None:
    publisher = FakePublisher()
    await make_service(publisher).publish("we decided X")

    (page, heading, text, summary) = publisher.started[0]
    assert page == "Meta talk:Community/Log"
    assert heading == "2026-07-10"  # coarse date only (spec section 9)
    assert text == ": [sanitized]we decided X"
    assert summary == "Log entry via Blybot"


async def test_two_logs_never_share_a_section() -> None:
    publisher = FakePublisher()
    service = make_service(publisher)
    await service.publish("first")
    await service.publish("second")
    assert len(publisher.started) == 2  # start_discussion always opens a new section
    assert publisher.continued == []


async def test_sanitizer_runs_before_publication() -> None:
    """No path may reach the publisher without passing the sanitizer (spec R7)."""
    publisher = FakePublisher()
    await make_service(publisher).publish("{{Delete}}")
    assert publisher.started[0][2].count("[sanitized]") == 1


@pytest.mark.parametrize("raw", [None, "", "   \n\t "])
async def test_media_only_messages_are_declined(raw: str | None) -> None:
    """Media-only /log targets publish nothing (spec R2)."""
    publisher = FakePublisher()
    with pytest.raises(NothingToPublishError):
        await make_service(publisher).publish(raw)
    assert publisher.started == []


async def test_multi_line_messages_stay_one_discussion_line() -> None:
    publisher = FakePublisher()
    await make_service(publisher).publish("line one\nline two")
    assert publisher.started[0][2] == ": [sanitized]line one<br>line two"


async def test_none_granularity_uses_a_neutral_heading() -> None:
    publisher = FakePublisher()
    await make_service(publisher, TimestampGranularity.NONE).publish("hello")
    heading = publisher.started[0][1]
    assert heading == UNDATED_HEADING
    assert "2026" not in heading
