"""Discussion line and section heading rendering tests."""

from __future__ import annotations

from datetime import UTC, datetime

from blybot.domain.models import TimestampGranularity
from blybot.domain.rendering import discussion_line, file_link, section_heading, timestamp

NOON = datetime(2026, 7, 10, 12, 5, tzinfo=UTC)


def test_depth_controls_indentation() -> None:
    assert discussion_line(1, "hello") == ": hello"
    assert discussion_line(3, "hello") == "::: hello"


def test_signature_is_appended_as_a_discussion_sign_off() -> None:
    assert discussion_line(2, "hi", signature="Leia Vimes from Narnia") == (
        ":: hi --Leia Vimes from Narnia"
    )


def test_inner_newlines_cannot_break_the_indentation() -> None:
    assert discussion_line(2, "a\nb\nc") == ":: a<br>b<br>c"


def test_file_link_renders_trusted_upload_markup() -> None:
    assert file_link("Blybot_Anon_1.png") == "[[File:Blybot_Anon_1.png|thumb]]"


def test_timestamp_granularities() -> None:
    assert timestamp(NOON, TimestampGranularity.MINUTE) == "2026-07-10 - 12:05 UTC"
    assert timestamp(NOON, TimestampGranularity.DATE) == "2026-07-10"
    assert timestamp(NOON, TimestampGranularity.NONE) is None


def test_section_heading_composition() -> None:
    assert section_heading("2026-07-10 - 12:05 UTC", "Leia") == "2026-07-10 - 12:05 UTC : Leia"
    assert section_heading("2026-07-10", None) == "2026-07-10"
    assert section_heading(None, "Leia") == "Leia"
    assert section_heading(None, None) == "Log entry"
