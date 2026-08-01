"""IRC ⇄ :class:`Scope` edge.

An IRC address is just a channel name (``#wikimedia``), so the mapping is
the simplest of the three platforms: the name *is* the scope's channel
part, and ``thread`` is always empty because IRC has no threads.

Channel names are lower-cased on the way in. IRC compares them
case-insensitively, so ``#Foo`` and ``#foo`` are one channel — without
normalising, they would become two distinct scopes with two profiles and
two archives.
"""

from __future__ import annotations

from typing import Final

from blybot.domain.models import Scope

PLATFORM: Final = "irc"


def scope_of(channel: str) -> Scope:
    """Build the :class:`Scope` addressing an IRC channel."""
    return Scope(PLATFORM, channel.lower())


def nick_scope(nick: str) -> Scope:
    """A :class:`Scope` addressing one nick directly.

    Usable for an immediate reply, never as a durable delivery handle:
    ``durable_dm`` is False precisely because a nick can change hands.
    """
    return Scope(PLATFORM, nick.lower())


def irc_target(scope: Scope) -> str:
    """Convert a :class:`Scope` back into the PRIVMSG target."""
    return scope.channel
