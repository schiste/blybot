"""LogPublicationService tests (spec R2, R6, R8)."""

from __future__ import annotations

import pytest

from blybot.domain.models import TimestampGranularity
from blybot.services.publish import LogPublicationService, NothingToPublishError
from tests.fakes import FakeClock, FakePublisher, PassthroughSanitizer


def make_service(
    publisher: FakePublisher,
    granularity: TimestampGranularity = TimestampGranularity.DATE,
) -> LogPublicationService:
    return LogPublicationService(
        publisher=publisher,
        sanitizer=PassthroughSanitizer(),
        clock=FakeClock(),
        target_page="Meta:Community/Log",
        edit_summary="Log entry via Blybot",
        timestamp_granularity=granularity,
    )


async def test_publishes_sanitized_text_to_target_page() -> None:
    publisher = FakePublisher()
    await make_service(publisher).publish("we decided X")

    (page, text, summary) = publisher.appends[0]
    assert page == "Meta:Community/Log"
    assert "[sanitized]we decided X" in text
    assert summary == "Log entry via Blybot"


async def test_sanitizer_runs_before_publication() -> None:
    """No path may reach the publisher without passing the sanitizer (spec R7)."""
    publisher = FakePublisher()
    await make_service(publisher).publish("{{Delete}}")
    assert publisher.appends[0][1].count("[sanitized]") == 1


@pytest.mark.parametrize("raw", [None, "", "   \n\t "])
async def test_media_only_messages_are_declined(raw: str | None) -> None:
    """Media-only /log targets publish nothing (spec R2)."""
    publisher = FakePublisher()
    with pytest.raises(NothingToPublishError):
        await make_service(publisher).publish(raw)
    assert publisher.appends == []


async def test_date_granularity_stamps_date_only() -> None:
    """Coarse timestamps limit correlation with Telegram activity (spec section 9)."""
    publisher = FakePublisher()
    await make_service(publisher).publish("hello")
    entry = publisher.appends[0][1]
    assert "2026-07-10" in entry
    assert "12:00" not in entry


async def test_none_granularity_stamps_nothing() -> None:
    publisher = FakePublisher()
    await make_service(publisher, TimestampGranularity.NONE).publish("hello")
    assert "2026" not in publisher.appends[0][1]


async def test_entry_appends_on_a_fresh_line() -> None:
    """Appended entries must not merge into the page's last existing line."""
    publisher = FakePublisher()
    await make_service(publisher).publish("hello")
    assert publisher.appends[0][1].startswith("\n")
