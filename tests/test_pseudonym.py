"""Pseudonym invariants (spec R6). These hold for ANY factory implementation."""

from __future__ import annotations

import inspect
import re

import blybot.domain.pseudonym
from blybot.domain.pseudonym import FIRST_NAMES, LOCATIONS, SURNAMES, RandomPseudonymFactory


def test_mint_takes_no_user_input() -> None:
    """A pseudonym must never be derivable from a user attribute (spec R6).

    Enforced structurally: ``mint()`` accepts no arguments, so no user ID
    can possibly flow into the pseudonym.
    """
    signature = inspect.signature(RandomPseudonymFactory.mint)
    assert list(signature.parameters) == ["self"]


def test_format_is_first_surname_from_location() -> None:
    factory = RandomPseudonymFactory()
    for _ in range(30):
        match = re.fullmatch(r"(\S+) (\S+) from (\S+)", factory.mint().value)
        assert match is not None
        first, surname, location = match.groups()
        assert first in FIRST_NAMES
        assert surname in SURNAMES
        assert location in LOCATIONS


def test_pseudonym_is_a_sane_wiki_heading_and_anchor() -> None:
    """Must be safe as a section heading AND as a #anchor in links.

    Spaces are the only character MediaWiki rewrites in anchors (to
    underscores, handled by page_for); anything else would break the
    page#anchor links the bot hands to DM users.
    """
    factory = RandomPseudonymFactory()
    for _ in range(50):
        value = factory.mint().value
        assert value
        assert len(value) <= 40
        assert all(char.isalnum() or char in " -" for char in value)


def test_mints_vary() -> None:
    """Draws come from an 8,000-combination space; 50 mints collapsing to
    fewer than 10 distinct values is astronomically improbable."""
    factory = RandomPseudonymFactory()
    minted = {factory.mint().value for _ in range(50)}
    assert len(minted) >= 10


def test_module_does_not_use_non_cryptographic_randomness() -> None:
    """The factory must draw from ``secrets`` (CSPRNG), never ``random``."""
    source = inspect.getsource(blybot.domain.pseudonym)
    assert "import secrets" in source
    assert "import random" not in source
