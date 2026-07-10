"""Value-object invariants."""

from __future__ import annotations

import pytest

from blybot.domain.models import LogEntry, Pseudonym


@pytest.mark.parametrize("blank", ["", "   ", "\n\t"])
def test_log_entry_rejects_blank_text(blank: str) -> None:
    with pytest.raises(ValueError, match="non-empty"):
        LogEntry(text=blank)


def test_pseudonym_rejects_empty_value() -> None:
    with pytest.raises(ValueError, match="non-empty"):
        Pseudonym("")


def test_value_objects_are_immutable() -> None:
    entry = LogEntry(text="hello")
    with pytest.raises(AttributeError):
        entry.text = "tampered"  # type: ignore[misc]
