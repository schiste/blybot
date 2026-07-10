"""Wikitext neutralization (spec R7).

Strategy: entity-encode every character that can alter page structure,
rather than wrapping in ``<nowiki>``. A wrapper can be escaped by a
payload containing ``</nowiki>``; character-level encoding has no wrapper
to escape. MediaWiki renders the entities back as the literal characters,
so published text reads exactly as written.

Neutralized constructs (per R7 acceptance criteria):

* templates / transclusion — ``{{...}}`` via ``{`` and ``}``
* links and categories — ``[[Category:...]]`` via ``[`` and ``]``
* signatures — ``~~~~`` via ``~``
* headings — ``== ... ==`` via ``=``
* tables and parameter pipes — via ``|``
* raw HTML / parser extension tags — via ``<`` and ``>``
* list / indent / definition line-starts — leading ``*`` ``#`` ``:`` ``;``
* preformatted blocks — leading spaces
"""

from __future__ import annotations

import re
from typing import Final

_CHAR_ENTITIES: Final[dict[str, str]] = {
    "&": "&amp;",  # first, so later entities are not double-encoded
    "{": "&#123;",
    "}": "&#125;",
    "[": "&#91;",
    "]": "&#93;",
    "<": "&lt;",
    ">": "&gt;",
    "|": "&#124;",
    "=": "&#61;",
    "~": "&#126;",
    "'": "&#39;",  # defuses '''bold'''/''italic'' runs
}

_TRANSLATION: Final = str.maketrans(_CHAR_ENTITIES)

# Line-leading characters that trigger block markup in MediaWiki.
_LINE_START: Final = re.compile(r"^[*#:; ]", flags=re.MULTILINE)


def _encode_line_start(match: re.Match[str]) -> str:
    char = match.group(0)
    return "&#32;" if char == " " else f"&#{ord(char)};"


class WikitextSanitizer:
    """Default :class:`blybot.domain.ports.Sanitizer` implementation."""

    def sanitize(self, text: str) -> str:
        """Return ``text`` with all structure-altering wikitext neutralized."""
        neutral = text.translate(_TRANSLATION)
        return _LINE_START.sub(_encode_line_start, neutral)
