"""Value-object invariants."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from blybot.domain.models import LogContent, LogMedia, Pseudonym, Session


def test_pseudonym_rejects_empty_value() -> None:
    with pytest.raises(ValueError, match="non-empty"):
        Pseudonym("")


def test_log_media_rejects_empty_content_or_type() -> None:
    with pytest.raises(ValueError, match="content"):
        LogMedia(content=b"", content_type="image/png")
    with pytest.raises(ValueError, match="content_type"):
        LogMedia(content=b"x", content_type="")


def test_log_content_presence_accepts_text_or_media() -> None:
    assert LogContent().has_publishable_content is False
    assert LogContent(text="  ").has_publishable_content is False
    assert LogContent(text="hello").has_publishable_content is True
    media_content = LogContent(media=(LogMedia(content=b"x", content_type="image/png"),))
    assert media_content.has_publishable_content


def test_value_objects_are_immutable() -> None:
    session = Session(
        pseudonym=Pseudonym("Anon-1"),
        anchor="Anon-1",
        last_seen=datetime(2026, 7, 10, tzinfo=UTC),
        created_at=datetime(2026, 7, 10, tzinfo=UTC),
    )
    with pytest.raises(AttributeError):
        session.anchor = "tampered"  # type: ignore[misc]
