"""Mirroring a conversation across platforms: the neutral relay core.

The bot's other two roles are **reducers** — many messages in, one
anonymized artifact out, on a delay. This one is a **relay**: one message
in, one message out per linked channel, immediately, with the author's
name preserved. It therefore uses the
:class:`~blybot.domain.ports.Transport` seam and none of the
``Source -> Transform -> Sink`` engine, which is built around polling an
archive window.

Everything here is pure: :meth:`BridgeService.relay` takes a
:class:`~blybot.domain.bridge.RelayMessage` and returns the
:class:`~blybot.domain.models.OutboundMessage`\\ s to send. No I/O, no
platform SDK, no storage.

**A bridge is a set of scopes, not a graph of pairwise links.** That is
what makes relay loops structurally impossible rather than merely
guarded: fan-out is "every scope in my group except me", so there is no
A -> B -> C -> A path to traverse. Membership lives in the profile store,
written one scope at a time by that scope's own admin (#81), so the
targets are resolved per message by the router and handed here — this
stays pure and synchronous.

The remaining loop is a relayed line finding its way back in as fresh
input — a bouncer echo, or a second bot instance. The fence for that is
**who wrote it**, not what it looks like: ``bot_authors`` names the bot on
each platform and a message from itself is never relayed. Sniffing the
text for an attribution prefix was the first attempt and is wrong, because
display names contain spaces: no anchored pattern can separate
``Alice Smith (discord): hi`` from ``I saw alice (discord): hi``.

**Long messages are deliberately not chunked here.** A 4096-character
Telegram message relayed to IRC is emitted as one ``OutboundMessage``;
the IRC transport already splits by encoded bytes at the protocol line
limit. Splitting here as well would only decide where the ``alice
(discord):`` prefix repeats — and letting the transport do it gives the
behavior a reader wants anyway: attribution once, then continuation
lines.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Final

from blybot.domain.models import OutboundMessage
from blybot.observability import log_event

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from blybot.domain.bridge import RelayMessage
    from blybot.domain.models import Scope
    from blybot.observability import Counters

# ``alice (discord): hello`` — the wire format for a mirrored line.
_ATTRIBUTION: Final = "{author} ({platform}): {text}"


@dataclass(eq=False)
class BridgeService:
    """Fans one message out to every other channel of its bridge."""

    counters: Counters
    # The bot's own display name per platform, so its own relayed output is
    # never mistaken for someone talking. Defense in depth: the adapters
    # already skip their own messages, but that is each adapter's promise
    # rather than the bridge's guarantee.
    bot_authors: Mapping[str, str] = field(default_factory=dict)

    def _is_own(self, message: RelayMessage) -> bool:
        """Whether this message was written by the bot on its own platform."""
        mine = self.bot_authors.get(message.origin.platform, "")
        return bool(mine) and mine.casefold() == message.author.casefold()

    def relay(self, message: RelayMessage, targets: Sequence[Scope]) -> tuple[OutboundMessage, ...]:
        """Render ``message`` for each of ``targets``.

        ``targets`` is the origin's bridge minus itself, resolved by the
        caller — which is where the store lives.
        """
        if not message.text.strip():
            return ()  # a media-only or empty line has nothing to mirror
        if self._is_own(message):
            # Our own output coming back, not a person talking. Relaying it
            # would compound the attribution prefix on every lap.
            self.counters.increment("bridge_echo_ignored")
            log_event("bridge_relay", "ignored", reason="echo")
            return ()
        if not targets:
            return ()
        text = _ATTRIBUTION.format(
            author=message.author,
            platform=message.origin.platform,
            text=message.text,
        )
        self.counters.increment("bridge_relayed")
        return tuple(OutboundMessage(scope=target, text=text) for target in targets)
