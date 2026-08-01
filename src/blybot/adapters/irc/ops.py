"""Live channel-operator tracking: IRC's answer to "is this user an admin?".

Telegram asks ``get_chat_member``; Discord reads ``guild_permissions``.
IRC has no such call — a bot learns who holds ``@`` by watching the
protocol and remembering. This module is that memory, and nothing else:

* it holds **raw nicks**, so it lives in the adapter and never crosses
  inward. Nothing here is written to the archive or the profile store —
  the pseudonymizing boundary (R6) sits between this and every service.
* it is **in-memory only**. A restart re-learns from the ``NAMES`` reply
  the server sends on JOIN, so there is no persisted identity to leak,
  and no stale grant surviving a redeploy.

Failure direction matters: when the tracker does not know about a nick it
answers **not an op**. A missed ``MODE`` line therefore costs an operator
one re-op, rather than handing an ordinary user the admin surface.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Final

# Prefixes a server may attach to a nick in a NAMES reply. Only the
# operator ones grant authority here; voice (`+`) is explicitly not admin.
_OP_PREFIXES: Final = ("@", "&", "~")
_ALL_PREFIXES: Final = (*_OP_PREFIXES, "%", "+", "!")

# Mode flags that grant channel authority, matching the prefixes above:
# ``o`` (op, ``@``), ``a`` (protected/admin, ``&``), ``q`` (owner, ``~``).
# Voice (``v``) and halfop (``h``) deliberately do NOT: neither conveys the
# authority that changing this channel's archiving settings requires.
_OP_FLAGS: Final = frozenset("oaq")
# Every flag that consumes one argument in a MODE change. ``l`` does so
# only when being set, and is handled separately at the call site.
_ARG_FLAGS: Final = frozenset("oaqvhkbeI")


def strip_prefix(entry: str) -> tuple[str, bool]:
    """Split a NAMES entry into ``(nick, is_op)``.

    Servers may stack prefixes (``@+nick``), and which ones they use is
    network-specific, so this reads every leading prefix character and
    reports whether any of them conveys operator status.
    """
    is_op = False
    index = 0
    while index < len(entry) and entry[index] in _ALL_PREFIXES:
        is_op = is_op or entry[index] in _OP_PREFIXES
        index += 1
    return entry[index:], is_op


@dataclass(eq=False)
class ChannelOps:
    """Who currently holds operator status, per channel.

    Nicks and channels are compared case-insensitively, as the protocol
    does: ``#Foo``/``#foo`` and ``Alice``/``alice`` are one entity, and
    treating them as two would let a re-cased nick escape a de-op.
    """

    _ops: dict[str, set[str]] = field(default_factory=dict, init=False)

    def is_op(self, channel: str, nick: str) -> bool:
        """Return whether ``nick`` is a known operator of ``channel``."""
        return nick.casefold() in self._ops.get(channel.casefold(), set())

    def grant(self, channel: str, nick: str) -> None:
        """Record ``nick`` as an operator of ``channel``."""
        self._ops.setdefault(channel.casefold(), set()).add(nick.casefold())

    def revoke(self, channel: str, nick: str) -> None:
        """Drop ``nick``'s operator status in ``channel``, if any."""
        self._ops.get(channel.casefold(), set()).discard(nick.casefold())

    def forget(self, nick: str) -> None:
        """Drop ``nick`` everywhere — they quit the network, or changed nick.

        A nick that leaves must not keep its grant: on IRC the name is the
        only handle, and a freed nick can be claimed by someone else.
        """
        folded = nick.casefold()
        for holders in self._ops.values():
            holders.discard(folded)

    def forget_channel(self, channel: str) -> None:
        """Drop every grant in ``channel`` — the bot has left it."""
        self._ops.pop(channel.casefold(), None)

    def replace(self, channel: str, entries: list[str]) -> None:
        """Replace ``channel``'s operator set from raw NAMES entries.

        A NAMES reply is the authoritative snapshot the server sends on
        JOIN; applying it wholesale is what lets a restart recover without
        persisting anything.
        """
        ops = {nick.casefold() for nick, is_op in map(strip_prefix, entries) if is_op}
        self._ops[channel.casefold()] = ops

    def apply_mode(self, channel: str, params: list[str]) -> None:
        """Apply one MODE change's parameters to ``channel``.

        ``params`` is the mode string followed by its arguments, exactly as
        the server sent it: ``["+oo", "alice", "bob"]``, ``["-o", "carol"]``,
        or a mix like ``["+o-o", "alice", "bob"]``.

        Pairing flags to arguments is the whole difficulty, because which
        flags consume one is network-specific, and mis-pairing here would
        grant operator status to *the wrong nick* — the one failure
        direction that actually matters. So this parses the line, and then
        applies the result **only if** its pairing consumed exactly the
        arguments the server sent. A line we modelled wrongly leaves the
        count mismatched and is discarded whole, costing an operator a
        re-op rather than handing out authority by accident.
        """
        if not params:
            return
        pending = self._parse_mode(params[0], params[1:])
        if pending is None:
            return
        for nick, adding in pending:
            (self.grant if adding else self.revoke)(channel, nick)

    @staticmethod
    def _parse_mode(flags: str, arguments: list[str]) -> list[tuple[str, bool]] | None:
        """Pair ``flags`` with ``arguments``; ``None`` if the pairing is unsound."""
        pending: list[tuple[str, bool]] = []
        adding, index = True, 0
        for flag in flags:
            if flag in "+-":
                adding = flag == "+"
                continue
            if flag not in _ARG_FLAGS and not (flag == "l" and adding):
                continue  # a no-argument channel mode (+m, +t, +i, …)
            if index >= len(arguments):
                return None  # truncated line
            if flag in _OP_FLAGS:
                pending.append((arguments[index], adding))
            index += 1
        if index != len(arguments):
            return None  # we mis-modelled this line; apply none of it
        return pending
