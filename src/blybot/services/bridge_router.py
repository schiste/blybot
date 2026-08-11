"""Delivering relayed messages to the platform that owns each target.

:class:`~blybot.services.bridge.BridgeService` is pure: it decides *what*
to send and *where*. Something still has to hold the transports and
actually send, and that is the one piece of the bridge which can only
exist in a process running every adapter at once — the reason the unified
topology exists (#76).

Transports register themselves as their platform comes up, so the router
is usable before every adapter has finished starting: a message for a
platform that is not up yet is dropped with a logged reason rather than
queued, because a mirror that silently replays five-minute-old lines into
a live conversation is worse than one that admits the gap.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from blybot.domain.ports import (
    PermanentTransportError,
    RateLimited,
    TransientTransportError,
)
from blybot.observability import log_event

if TYPE_CHECKING:
    from blybot.domain.bridge import RelayMessage
    from blybot.domain.ports import Transport
    from blybot.services.bridge import BridgeService


@dataclass(eq=False)
class BridgeRouter:
    """Fans a relayed message out and sends each copy on its own transport."""

    bridge: BridgeService
    transports: dict[str, Transport] = field(default_factory=dict)

    def register(self, platform: str, transport: Transport) -> None:
        """Make ``platform`` reachable; called as each adapter comes up."""
        self.transports[platform] = transport

    async def dispatch(self, message: RelayMessage) -> None:
        """Relay one inbound message to every other channel of its bridge.

        Never raises: an inbound handler must not die because a *different*
        platform is unreachable, and one failed target must not stop the
        others. Each outcome is logged with the reason rather than
        swallowed.
        """
        for outbound in self.bridge.relay(message):
            transport = self.transports.get(outbound.scope.platform)
            if transport is None:
                log_event("bridge_deliver", "ignored", reason="platform_down")
                continue
            try:
                await transport.send(outbound)
            except RateLimited:
                # The pacing queue (#80) owns waiting this out; here it can
                # only mean the transport itself refused right now.
                log_event("bridge_deliver", "ignored", reason="flood")
            except TransientTransportError:
                log_event("bridge_deliver", "ignored", reason="transient")
            except PermanentTransportError:
                log_event("bridge_deliver", "ignored", reason="permanent")
            except Exception as error:  # keep the inbound handler alive
                log_event("bridge_deliver", "error", error=type(error).__name__)
