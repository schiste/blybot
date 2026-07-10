"""Discussion line rendering tests."""

from __future__ import annotations

from blybot.domain.rendering import discussion_line


def test_depth_controls_indentation() -> None:
    assert discussion_line(1, "hello") == ": hello"
    assert discussion_line(3, "hello") == "::: hello"


def test_inner_newlines_cannot_break_the_indentation() -> None:
    assert discussion_line(2, "a\nb\nc") == ":: a<br>b<br>c"
