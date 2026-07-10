"""Pseudonym minting (spec R6, section 10).

Hard requirements, enforced by tests:

* randomness comes from :mod:`secrets` (CSPRNG) — never :mod:`random`;
* the mint takes **no input**: a pseudonym must not be derived from a
  Telegram user ID or any other user attribute, so reversal or
  cross-session linkage is impossible even for the operator.

Format: ``<first name> <surname> from <location>``, all drawn from
fantasy / science-fiction classics — a readable byline for a talk page
("as Trillian Baggins from Gallifrey said above..."). The combined
namespace is 20x20x20 = 8,000; :class:`~blybot.services.sessions.
SessionRegistry` refuses to mint a pseudonym that another *live*
session is using, so the only effect of a repeat across history is a
duplicated section heading on the archive page.
"""

from __future__ import annotations

import secrets
from typing import Final

from blybot.domain.models import Pseudonym

FIRST_NAMES: Final = (
    "Arwen",
    "Leia",
    "Frodo",
    "Paul",
    "Hermione",
    "Ender",
    "Ripley",
    "Trillian",
    "Geralt",
    "Lyra",
    "Kvothe",
    "Hari",
    "Essun",
    "Binti",
    "Hiro",
    "Cordelia",
    "Luke",
    "Daenerys",
    "Morpheus",
    "Zaphod",
)

SURNAMES: Final = (
    "Skywalker",
    "Baggins",
    "Atreides",
    "Granger",
    "Stark",
    "Targaryen",
    "Solo",
    "Kenobi",
    "Seldon",
    "Vimes",
    "Weasley",
    "Took",
    "Wiggin",
    "Everdeen",
    "Vorkosigan",
    "Dent",
    "Beeblebrox",
    "Snow",
    "Mormont",
    "Strange",
)

LOCATIONS: Final = (
    "Arrakis",
    "Mordor",
    "Rivendell",
    "Hogwarts",
    "Winterfell",
    "Trantor",
    "Gallifrey",
    "Tatooine",
    "Narnia",
    "Ankh-Morpork",
    "Coruscant",
    "Vulcan",
    "Gondor",
    "Camelot",
    "Avalon",
    "Solaris",
    "Hyperion",
    "Terminus",
    "Caladan",
    "Lankhmar",
)


class RandomPseudonymFactory:
    """Default :class:`blybot.domain.ports.PseudonymFactory` implementation."""

    def mint(self) -> Pseudonym:
        """Return a fresh pseudonym, independent of all previous mints."""
        return Pseudonym(
            f"{secrets.choice(FIRST_NAMES)} {secrets.choice(SURNAMES)} "
            f"from {secrets.choice(LOCATIONS)}"
        )
