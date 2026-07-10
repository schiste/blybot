"""Value-object invariants."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from blybot.domain.models import Pseudonym, Session


def test_pseudonym_rejects_empty_value() -> None:
    with pytest.raises(ValueError, match="non-empty"):
        Pseudonym("")


def test_value_objects_are_immutable() -> None:
    session = Session(
        pseudonym=Pseudonym("Anon-1"),
        anchor="Anon-1",
        last_seen=datetime(2026, 7, 10, tzinfo=UTC),
    )
    with pytest.raises(AttributeError):
        session.anchor = "tampered"  # type: ignore[misc]
