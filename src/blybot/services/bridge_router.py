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
from functools import partial
from typing import TYPE_CHECKING, Any

from blybot.domain.models import OutboundMessage
from blybot.domain.ports import (
    PermanentTransportError,
    RateLimited,
    StorageError,
    TransientTransportError,
)
from blybot.observability import log_event
from blybot.services.relay_queue import RelayQueue

if TYPE_CHECKING:
    from collections.abc import Coroutine

    from blybot.domain.bridge import RelayMessage
    from blybot.domain.models import Scope
    from blybot.domain.ports import ProfileStore, Transport
    from blybot.services.bridge import BridgeService


@dataclass(eq=False)
class BridgeRouter:
    """Fans a relayed message out and sends each copy on its own transport."""

    bridge: BridgeService
    # Membership is stored per scope by its own admin (#81), so who a
    # message mirrors into is a *lookup*, not startup configuration — a
    # `bridge join` must take effect on the next message, not the next
    # restart.
    store: ProfileStore | None = None
    transports: dict[str, Transport] = field(default_factory=dict)
    # One backlog per platform, because the bottleneck is per-connection:
    # IRC's pacing has nothing to do with Discord's.
    queues: dict[str, RelayQueue] = field(default_factory=dict)

    def register(self, platform: str, transport: Transport) -> None:
        """Make ``platform`` reachable; called as each adapter comes up."""
        self.transports[platform] = transport
        self.queues[platform] = RelayQueue(
            platform=platform,
            send=partial(self._send, label="bridge_deliver"),
            notify=self.announce,
        )

    def workers(self) -> list[Coroutine[Any, Any, None]]:
        """The drain loops the runtime must run, one per registered platform."""
        return [queue.run() for queue in self.queues.values()]

    async def announce(self, scopes: list[Scope], text: str) -> None:
        """Tell each scope something about the bridge itself (#81).

        Unlike a relay this carries no author — it is the bot speaking —
        and it must never fail the command that triggered it, so a
        channel that cannot be reached is logged and skipped.

        Deliberately **not** queued: a bridge notice, and above all a
        "the relay is running behind" notice, is useless if it arrives
        behind the backlog it is about.
        """
        for scope in scopes:
            await self._send(OutboundMessage(scope=scope, text=text), label="bridge_announce")

    async def dispatch(self, message: RelayMessage) -> None:
        """Relay one inbound message to every other channel of its bridge.

        Never raises: an inbound handler must not die because a *different*
        platform is unreachable, and one failed target must not stop the
        others. Each outcome is logged with the reason rather than
        swallowed.
        """
        targets = await self._targets(message.origin)
        audience = [message.origin, *targets]
        for outbound in self.bridge.relay(message, targets):
            queue = self.queues.get(outbound.scope.platform)
            if queue is None:
                log_event("bridge_deliver", "ignored", reason="platform_down")
                continue
            # Queue rather than send: a slow platform must not stall the
            # inbound handler, and the backlog is what makes the lag
            # measurable and therefore announceable (#80).
            await queue.submit(outbound, audience)

    async def _targets(self, origin: Scope) -> tuple[Scope, ...]:
        """The other members of ``origin``'s bridge, or nothing.

        A storage outage means the mirror pauses rather than the inbound
        handler dying — the same failure posture as everything else that
        reads this store.
        """
        if self.store is None:
            return ()
        try:
            profile = await self.store.get(origin)
            if profile is None or not profile.bridge_id:
                return ()
            members = await self.store.list_bridge_members(profile.bridge_id)
        except StorageError:
            log_event("bridge_deliver", "ignored", reason="storage")
            return ()
        return tuple(member.scope for member in members if member.scope != origin)

    async def _send(self, outbound: OutboundMessage, *, label: str) -> None:
        """Deliver one message on its platform's transport, swallowing failure."""
        transport = self.transports.get(outbound.scope.platform)
        if transport is None:
            log_event(label, "ignored", reason="platform_down")
            return
        try:
            await transport.send(outbound)
        except RateLimited:
            # The pacing queue (#80) owns waiting this out; here it can
            # only mean the transport itself refused right now.
            log_event(label, "ignored", reason="flood")
        except TransientTransportError:
            log_event(label, "ignored", reason="transient")
        except PermanentTransportError:
            log_event(label, "ignored", reason="permanent")
        except Exception as error:  # keep the inbound handler alive
            log_event(label, "error", error=type(error).__name__)
