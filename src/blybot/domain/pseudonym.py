"""Pseudonym minting (spec R6, section 10).

Hard requirements, enforced by tests:

* randomness comes from :mod:`secrets` (CSPRNG) — never :mod:`random`;
* the mint takes **no input**: a pseudonym must not be derived from a
  Telegram user ID or any other user attribute, so reversal or
  cross-session linkage is impossible even for the operator.
"""

from __future__ import annotations

import secrets

from blybot.domain.models import Pseudonym


class RandomPseudonymFactory:
    """Default :class:`blybot.domain.ports.PseudonymFactory` implementation.

    The current format is ``Guest-<6 hex chars>`` (e.g. ``Guest-a3f01c``),
    which gives ~16.7 million combinations per prefix.

    TODO(design): the pseudonym *format* is a product decision, not a
    security one — any format is fine as long as ``mint()`` stays
    input-free and CSPRNG-backed. A human-friendly alternative
    (e.g. ``Amber-Heron-42`` from small word lists) reads better as a
    discussion byline on Meta and is easier for participants to refer to
    ("as Amber-Heron said above..."), at the cost of a smaller namespace
    per session-day. See CONTRIBUTING.md for the invariants any
    implementation must keep.
    """

    def mint(self) -> Pseudonym:
        """Return a fresh pseudonym, independent of all previous mints."""
        return Pseudonym(f"Guest-{secrets.token_hex(3)}")
