"""Pseudonym invariants (spec R6). These hold for ANY factory implementation."""

from __future__ import annotations

import inspect

import blybot.domain.pseudonym
from blybot.domain.pseudonym import RandomPseudonymFactory


def test_mint_takes_no_user_input() -> None:
    """A pseudonym must never be derivable from a user attribute (spec R6).

    Enforced structurally: ``mint()`` accepts no arguments, so no user ID
    can possibly flow into the pseudonym.
    """
    signature = inspect.signature(RandomPseudonymFactory.mint)
    assert list(signature.parameters) == ["self"]


def test_mints_are_distinct_across_calls() -> None:
    factory = RandomPseudonymFactory()
    minted = {factory.mint().value for _ in range(200)}
    assert len(minted) == 200


def test_pseudonym_is_a_sane_wiki_heading_and_anchor() -> None:
    """Must be safe as a section heading AND as a #anchor in links.

    Spaces or punctuation would be percent/underscore-mangled by
    MediaWiki's anchor encoding, breaking the page#anchor links the bot
    hands to DM users.
    """
    factory = RandomPseudonymFactory()
    for _ in range(50):
        value = factory.mint().value
        assert value
        assert len(value) <= 40
        assert all(char.isalnum() or char == "-" for char in value)


def test_module_does_not_use_non_cryptographic_randomness() -> None:
    """The factory must draw from ``secrets`` (CSPRNG), never ``random``."""
    source = inspect.getsource(blybot.domain.pseudonym)
    assert "import secrets" in source
    assert "import random" not in source
