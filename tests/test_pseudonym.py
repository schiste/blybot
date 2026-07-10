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


def test_pseudonym_is_a_sane_wiki_byline() -> None:
    """Must be safe to embed as a discussion byline without re-sanitizing."""
    factory = RandomPseudonymFactory()
    for _ in range(50):
        value = factory.mint().value
        assert value
        assert len(value) <= 40
        assert not set(value) & set("{}[]<>|=~\n")


def test_module_does_not_use_non_cryptographic_randomness() -> None:
    """The factory must draw from ``secrets`` (CSPRNG), never ``random``."""
    source = inspect.getsource(blybot.domain.pseudonym)
    assert "import secrets" in source
    assert "import random" not in source
