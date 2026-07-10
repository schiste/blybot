"""Composition of talk-page discussion wikitext.

The markup added here (indentation colons, ``<br>``) is bot-supplied and
trusted; it is applied *after* user text has passed the sanitizer, which
is what keeps the composition safe.
"""

from __future__ import annotations


def discussion_line(depth: int, text: str) -> str:
    """Render one indented discussion line.

    ``depth`` is the message's 1-based ordinal in its exchange — each
    reply indents one level deeper, the wiki convention for a
    back-and-forth. Newlines inside the message become ``<br>`` so a
    multi-line message stays one discussion line and the indentation
    cannot be broken.
    """
    return ":" * depth + " " + text.replace("\n", "<br>")
