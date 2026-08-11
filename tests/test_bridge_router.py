"""The router: fan-out onto real transports, and its failure isolation (#78)."""

from __future__ import annotations

from datetime import timedelta
from typing import TYPE_CHECKING

from blybot.adapters.telegram.transport import TELEGRAM_CAPABILITIES
from blybot.domain.bridge import RelayMessage
from blybot.domain.models import Scope
from blybot.domain.ports import (
    PermanentTransportError,
    RateLimited,
    TransientTransportError,
)
from blybot.observability import Counters
from blybot.services.bridge import BridgeService
from blybot.services.bridge_router import BridgeRouter

if TYPE_CHECKING:
    from blybot.domain.models import OutboundMessage

TG = Scope("telegram", "-100500")
DC = Scope("discord", "900100")
IRC = Scope("irc", "#wikipedia-fr")


class _RecordingTransport:
    capabilities = TELEGRAM_CAPABILITIES  # any; the router never reads it

    def __init__(self, error: Exception | None = None) -> None:
        self.sent: list[OutboundMessage] = []
        self._error = error

    async def send(self, message: OutboundMessage) -> None:
        if self._error is not None:
            raise self._error
        self.sent.append(message)


def _router(**transports: _RecordingTransport) -> BridgeRouter:
    bridge = BridgeService(groups=(frozenset({TG, DC, IRC}),), counters=Counters())
    router = BridgeRouter(bridge=bridge)
    for platform, transport in transports.items():
        router.register(platform, transport)
    return router


async def test_each_copy_goes_out_on_its_own_platforms_transport() -> None:
    telegram, irc = _RecordingTransport(), _RecordingTransport()
    router = _router(telegram=telegram, irc=irc)

    await router.dispatch(RelayMessage(origin=DC, author="alice", text="hello"))

    assert [message.scope for message in telegram.sent] == [TG]
    assert [message.scope for message in irc.sent] == [IRC]
    assert telegram.sent[0].text == "alice (discord): hello"


async def test_a_platform_that_has_not_started_is_skipped_not_queued() -> None:
    """A mirror replaying old lines into a live conversation is worse than
    one that admits the gap."""
    telegram = _RecordingTransport()
    router = _router(telegram=telegram)  # IRC never registered

    await router.dispatch(RelayMessage(origin=DC, author="alice", text="hello"))

    assert len(telegram.sent) == 1  # the reachable platform still got it


async def test_one_unreachable_target_never_stops_the_others() -> None:
    """An inbound handler must not die because a *different* platform is
    broken, and one bad target must not silence the rest."""
    for failure in (
        RateLimited(retry_after=timedelta(seconds=5)),
        TransientTransportError(),
        PermanentTransportError(),
        RuntimeError("the SDK did something unexpected"),
    ):
        telegram = _RecordingTransport()
        router = _router(telegram=telegram, irc=_RecordingTransport(error=failure))

        await router.dispatch(RelayMessage(origin=DC, author="alice", text="hello"))

        assert [message.scope for message in telegram.sent] == [TG], failure


async def test_an_unbridged_scope_dispatches_nothing() -> None:
    telegram = _RecordingTransport()
    bridge = BridgeService(groups=(frozenset({DC, IRC}),), counters=Counters())
    router = BridgeRouter(bridge=bridge)
    router.register("telegram", telegram)

    await router.dispatch(RelayMessage(origin=TG, author="alice", text="hello"))

    assert telegram.sent == []
