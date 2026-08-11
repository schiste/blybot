"""The router: fan-out onto real transports, and its failure isolation (#78)."""

from __future__ import annotations

import asyncio
from datetime import timedelta
from typing import TYPE_CHECKING

from blybot.adapters.telegram.transport import TELEGRAM_CAPABILITIES
from blybot.domain.bridge import RelayMessage
from blybot.domain.models import GroupProfile, Scope
from blybot.domain.ports import (
    PermanentTransportError,
    RateLimited,
    TransientTransportError,
)
from blybot.observability import Counters
from blybot.services.bridge import BridgeService
from blybot.services.bridge_router import BridgeRouter
from tests.fakes import InMemoryProfiles

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


async def _dispatch(router: BridgeRouter, message: RelayMessage) -> None:
    """Dispatch and let the per-platform drains finish (#80)."""
    await router.dispatch(message)
    workers = [asyncio.ensure_future(worker) for worker in router.workers()]
    for queue in router.queues.values():
        await queue.drained()
    for worker in workers:
        worker.cancel()
    await asyncio.gather(*workers, return_exceptions=True)


def _store(*members: Scope) -> InMemoryProfiles:
    """A store where every listed scope has joined one bridge."""
    return InMemoryProfiles(
        profiles={scope: GroupProfile(scope=scope, bridge_id="abc") for scope in members}
    )


def _router(
    store: InMemoryProfiles | None = None, **transports: _RecordingTransport
) -> BridgeRouter:
    router = BridgeRouter(
        bridge=BridgeService(counters=Counters()),
        store=store if store is not None else _store(TG, DC, IRC),
    )
    for platform, transport in transports.items():
        router.register(platform, transport)
    return router


async def test_each_copy_goes_out_on_its_own_platforms_transport() -> None:
    telegram, irc = _RecordingTransport(), _RecordingTransport()
    router = _router(telegram=telegram, irc=irc)

    await _dispatch(router, RelayMessage(origin=DC, author="alice", text="hello"))

    assert [message.scope for message in telegram.sent] == [TG]
    assert [message.scope for message in irc.sent] == [IRC]
    assert telegram.sent[0].text == "alice (discord): hello"


async def test_a_platform_that_has_not_started_is_skipped_not_queued() -> None:
    """A mirror replaying old lines into a live conversation is worse than
    one that admits the gap."""
    telegram = _RecordingTransport()
    router = _router(telegram=telegram)  # IRC never registered

    await _dispatch(router, RelayMessage(origin=DC, author="alice", text="hello"))

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

        await _dispatch(router, RelayMessage(origin=DC, author="alice", text="hello"))

        assert [message.scope for message in telegram.sent] == [TG], failure


async def test_an_unbridged_scope_dispatches_nothing() -> None:
    telegram = _RecordingTransport()
    router = _router(_store(DC, IRC), telegram=telegram)

    await _dispatch(router, RelayMessage(origin=TG, author="alice", text="hello"))

    assert telegram.sent == []


async def test_an_announcement_reaches_every_named_channel() -> None:
    """Bridge notices carry no author — the bot is speaking, not relaying."""
    telegram, irc = _RecordingTransport(), _RecordingTransport()
    router = _router(telegram=telegram, irc=irc)

    await router.announce([TG, IRC], "alice joined the bridge")

    assert telegram.sent[0].text == "alice joined the bridge"
    assert irc.sent[0].text == "alice joined the bridge"


async def test_an_unreachable_channel_never_fails_the_command_that_caused_it() -> None:
    telegram = _RecordingTransport()
    router = _router(telegram=telegram, irc=_RecordingTransport(error=PermanentTransportError()))

    await router.announce([TG, IRC], "notice")  # must not raise

    assert len(telegram.sent) == 1


async def test_a_bridge_join_takes_effect_on_the_next_message() -> None:
    """Membership is a lookup, not startup configuration (#81): an admin
    joining must not have to wait for a restart."""
    telegram, irc = _RecordingTransport(), _RecordingTransport()
    store = _store(DC, TG)
    router = _router(store, telegram=telegram, irc=irc)

    await _dispatch(router, RelayMessage(origin=DC, author="alice", text="one"))
    assert (len(telegram.sent), len(irc.sent)) == (1, 0)

    store.profiles[IRC] = GroupProfile(scope=IRC, bridge_id="abc")  # `bridge join`
    await _dispatch(router, RelayMessage(origin=DC, author="alice", text="two"))
    assert (len(telegram.sent), len(irc.sent)) == (2, 1)


async def test_an_unconfigured_or_unbridged_scope_resolves_no_targets() -> None:
    telegram = _RecordingTransport()
    router = BridgeRouter(bridge=BridgeService(counters=Counters()))  # no store at all
    router.register("telegram", telegram)
    await _dispatch(router, RelayMessage(origin=DC, author="alice", text="hi"))
    assert telegram.sent == []

    # ...and a scope whose profile exists but has joined nothing.
    unjoined = InMemoryProfiles(profiles={DC: GroupProfile(scope=DC)})
    await _dispatch(
        _router(unjoined, telegram=telegram), RelayMessage(origin=DC, author="alice", text="hi")
    )
    assert telegram.sent == []


async def test_a_storage_outage_pauses_the_mirror_rather_than_killing_the_handler() -> None:
    telegram = _RecordingTransport()
    store = _store(DC, TG)
    store.fail = True
    router = _router(store, telegram=telegram)

    await _dispatch(router, RelayMessage(origin=DC, author="alice", text="hi"))  # must not raise

    assert telegram.sent == []


async def test_announcing_to_a_platform_that_never_came_up_is_skipped() -> None:
    """The bridge notices must not fail because one platform is missing."""
    telegram = _RecordingTransport()
    router = _router(telegram=telegram)  # IRC never registered
    await router.announce([TG, IRC], "notice")  # must not raise
    assert len(telegram.sent) == 1


async def test_a_relay_for_a_platform_that_never_came_up_is_skipped() -> None:
    """Registration creates the queue, so an unregistered platform has none."""
    telegram = _RecordingTransport()
    router = _router(telegram=telegram)

    await _dispatch(router, RelayMessage(origin=DC, author="alice", text="hi"))

    assert [message.scope for message in telegram.sent] == [TG]  # IRC simply skipped
