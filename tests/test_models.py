"""Value-object invariants."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from blybot.domain.models import (
    CommandResult,
    LogContent,
    LogMedia,
    PlatformCapabilities,
    Pseudonym,
    Session,
)
from blybot.domain.ports import RateLimited


def test_platform_capabilities_construct_and_default_off() -> None:
    caps = PlatformCapabilities(max_message_chars=4096)
    assert caps.max_message_chars == 4096
    # Every optional capability defaults to off; a bare cap grants nothing.
    assert not any(
        (
            caps.threads,
            caps.durable_dm,
            caps.deep_links,
            caps.bot_can_open_dm,
            caps.message_delete,
            caps.id_can_change,
            caps.rich_choices,
        )
    )
    rich = PlatformCapabilities(max_message_chars=2000, threads=True, durable_dm=True)
    assert rich.threads is True
    assert rich.durable_dm is True


def test_platform_capabilities_rejects_nonpositive_cap() -> None:
    with pytest.raises(ValueError, match="max_message_chars"):
        PlatformCapabilities(max_message_chars=0)


def test_command_result_carries_text_and_rejects_empty() -> None:
    assert CommandResult(text="done").text == "done"
    with pytest.raises(ValueError, match="non-empty"):
        CommandResult(text="")


def test_rate_limited_carries_its_retry_after() -> None:
    error = RateLimited(timedelta(seconds=5))
    assert error.retry_after == timedelta(seconds=5)


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
