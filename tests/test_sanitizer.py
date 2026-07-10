"""Sanitizer tests: the executable form of spec R7's acceptance criteria."""

from __future__ import annotations

import pytest

from blybot.domain.sanitizer import WikitextSanitizer


@pytest.fixture
def sanitizer() -> WikitextSanitizer:
    return WikitextSanitizer()


# The four constructs called out verbatim in R7's acceptance criteria.
R7_PAYLOADS = [
    "{{Delete}}",
    "[[Category:Foo]]",
    "== Heading ==",
    "~~~~",
]


@pytest.mark.parametrize("payload", R7_PAYLOADS)
def test_r7_acceptance_payloads_are_neutralized(sanitizer: WikitextSanitizer, payload: str) -> None:
    result = sanitizer.sanitize(payload)
    for char in "{}[]=~":
        assert char not in result


def test_table_and_pipe_syntax_is_neutralized(sanitizer: WikitextSanitizer) -> None:
    result = sanitizer.sanitize("{| class=x\n|-\n| cell\n|}")
    assert "|" not in result
    assert "{" not in result


def test_html_and_parser_tags_are_neutralized(sanitizer: WikitextSanitizer) -> None:
    result = sanitizer.sanitize("<div>x</div><ref>y</ref>")
    assert "<" not in result
    assert ">" not in result


def test_nowiki_breakout_is_impossible(sanitizer: WikitextSanitizer) -> None:
    """A payload containing </nowiki> must not survive as live markup."""
    result = sanitizer.sanitize("</nowiki>{{Delete}}<nowiki>")
    assert "</nowiki>" not in result
    assert "{{" not in result


def test_line_leading_block_markup_is_neutralized(sanitizer: WikitextSanitizer) -> None:
    result = sanitizer.sanitize("* bullet\n# numbered\n: indent\n; term\n pre block")
    for line in result.splitlines():
        assert not line.startswith(("*", "#", ":", ";", " "))


def test_ampersand_is_encoded_first(sanitizer: WikitextSanitizer) -> None:
    """User-typed entities must render literally, not be smuggled through."""
    assert sanitizer.sanitize("&lt;") == "&amp;lt;"


def test_plain_prose_keeps_its_words(sanitizer: WikitextSanitizer) -> None:
    text = "We agreed to move the meeting to Thursday."
    assert sanitizer.sanitize(text) == text


def test_sanitize_is_idempotent_in_effect(sanitizer: WikitextSanitizer) -> None:
    """Double-sanitizing must still contain zero live markup characters."""
    once = sanitizer.sanitize("{{Delete}} [[Category:Foo]]")
    twice = sanitizer.sanitize(once)
    for char in "{}[]<>|=~":
        assert char not in twice
