"""Owner-configurable summary delivery (#69: #71, #72, #73).

The channel's owner picks *where* the recurring summary goes; a subscriber
either inherits that or overrides it with their own.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any, cast

import pytest

from blybot.adapters.irc.capabilities import IRC_CAPABILITIES
from blybot.adapters.telegram.transport import TELEGRAM_CAPABILITIES
from blybot.domain.models import (
    ActionContext,
    AnalysisReport,
    ConsentMode,
    Scope,
    StepSpec,
    TriggerKind,
)
from blybot.domain.subscriptions import Subscription
from blybot.observability import Counters
from blybot.services import commands as c
from blybot.services.actions import (
    ActionParseError,
    delivery_of,
    parse_action,
)
from blybot.services.analyze import ChatReplySink, SubscriberDmSink
from blybot.services.commands import CommandService
from blybot.services.directory import ChannelDirectory
from blybot.services.policy import GroupPolicy
from blybot.services.subscriptions import SubscriptionScheduler
from tests.fakes import (
    FakeClock,
    InMemoryActions,
    InMemoryProfiles,
    InMemorySubscriptions,
)

NOW = datetime(2026, 8, 11, 9, 0, tzinfo=UTC)
GROUP = Scope("telegram", "-100500")
ALICE = Scope("telegram", "777")
BOB = Scope("telegram", "888")


# --- the delivery= parameter (#72) --------------------------------------------


def test_an_action_without_delivery_publishes_exactly_as_before() -> None:
    """Every stored action predates the key, so the default must be today's."""
    spec = parse_action("daily@09:00 summarize")
    assert [step.name for step in spec.sinks] == ["wiki_section"]
    assert delivery_of(spec) == "wiki"


@pytest.mark.parametrize(
    ("mode", "sinks"),
    [
        ("wiki", ["wiki_section"]),
        ("wiki+subs", ["wiki_section", "subscriber_dm"]),
        ("subs", ["subscriber_dm"]),
    ],
)
def test_each_mode_selects_its_own_destinations(mode: str, sinks: list[str]) -> None:
    spec = parse_action(f"daily@09:00 summarize delivery={mode}")
    assert [step.name for step in spec.sinks] == sinks
    assert delivery_of(spec) == mode


def test_an_unknown_mode_names_the_valid_ones() -> None:
    with pytest.raises(ActionParseError, match="wiki, wiki\\+subs, subs"):
        parse_action("daily@09:00 summarize delivery=carrier-pigeon")


def test_delivery_survives_alongside_the_other_parameters() -> None:
    spec = parse_action("daily@09:00 summarize delivery=subs window=48h lang=fr")
    assert [step.name for step in spec.sinks] == ["subscriber_dm"]
    assert spec.source.param("window") == "48h"
    assert spec.transforms[0].param("lang") == "fr"


def _service(
    *,
    capabilities: object = TELEGRAM_CAPABILITIES,
    actions: InMemoryActions | None = None,
    subscriptions: InMemorySubscriptions | None = None,
    store: InMemoryProfiles | None = None,
) -> CommandService:
    profiles = store if store is not None else InMemoryProfiles()
    return CommandService(
        directory=ChannelDirectory(
            store=profiles,
            default_log_page="Project:Log",
            default_consent=ConsentMode.IMMEDIATE,
            default_repo="",
            page_suffix="logs",
        ),
        groups=GroupPolicy(allowed=set()),
        page_url_for=str,
        counters=Counters(),
        actions=actions if actions is not None else InMemoryActions(),
        subscriptions=subscriptions,
        clock=FakeClock(current=NOW),
        capabilities=capabilities,  # type: ignore[arg-type]
    )


async def test_a_subscriber_mode_is_refused_where_there_is_no_durable_dm() -> None:
    """Refuse rather than fall back: an admin who asked for subscriber
    delivery would otherwise believe they had it (#72)."""
    service = _service(capabilities=IRC_CAPABILITIES)

    refused = await service.add_action(
        GROUP, is_admin=True, spec="daily@09:00 summarize delivery=subs"
    )

    assert "durable direct messages" in refused.text
    assert refused.ok is False


async def test_the_wiki_mode_works_on_every_platform() -> None:
    service = _service(capabilities=IRC_CAPABILITIES)
    stored = await service.add_action(GROUP, is_admin=True, spec="daily@09:00 summarize")
    assert stored.ok is True


# --- the fan-out sink (#71) ---------------------------------------------------


def _report() -> AnalysisReport:
    return AnalysisReport(
        title="Summary",
        items=("something happened",),
        message_count=3,
        participant_count=2,
        since=NOW - timedelta(days=1),
        until=NOW,
        model_label="test-model",
        lang="en",
    )


def _sink(subs: InMemorySubscriptions, capabilities: object = TELEGRAM_CAPABILITIES) -> object:
    return SubscriberDmSink(
        subscriptions=subs,
        reply=ChatReplySink(max_chars=4096),
        capabilities=capabilities,  # type: ignore[arg-type]
        counters=Counters(),
    )


def _context() -> ActionContext:
    return ActionContext(scope=GROUP, spec=parse_action("daily@09:00 summarize"), now=NOW)


def _subscription(dm: Scope, *, scope: Scope = GROUP, inherited: bool = True) -> Subscription:
    return Subscription(
        sub_id=f"sub-{dm.channel}",
        dm=dm,
        scope=scope,
        schedule=parse_action("daily@09:00 summarize").trigger.schedule,  # type: ignore[arg-type]
        recipe="summarize",
        lang="en",
        inherited=inherited,
        last_run=NOW,
    )


async def test_one_report_is_addressed_to_every_inheriting_subscriber() -> None:
    subs = InMemorySubscriptions()
    await subs.add(_subscription(ALICE))
    await subs.add(_subscription(BOB))
    sink = _sink(subs)

    messages = await sink.deliver(_context(), StepSpec(name="subscriber_dm"), _report())  # type: ignore[attr-defined]

    assert {message.scope for message in messages} == {ALICE, BOB}
    # Rendered once and re-addressed: the same text reaches everyone.
    assert len({message.text for message in messages}) == 1


async def test_an_overriding_subscriber_is_not_served_by_the_fan_out() -> None:
    """They have their own run on their own cadence; sending here too
    would deliver the digest twice."""
    subs = InMemorySubscriptions()
    await subs.add(_subscription(ALICE, inherited=False))
    sink = _sink(subs)

    messages = await sink.deliver(_context(), StepSpec(name="subscriber_dm"), _report())  # type: ignore[attr-defined]

    assert messages == ()


async def test_another_channels_subscribers_are_not_served() -> None:
    subs = InMemorySubscriptions()
    await subs.add(_subscription(ALICE, scope=Scope("telegram", "-100999")))
    sink = _sink(subs)

    messages = await sink.deliver(_context(), StepSpec(name="subscriber_dm"), _report())  # type: ignore[attr-defined]

    assert messages == ()


async def test_the_fan_out_is_gated_on_durable_dm() -> None:
    subs = InMemorySubscriptions()
    await subs.add(_subscription(ALICE))
    sink = _sink(subs, IRC_CAPABILITIES)

    assert await sink.deliver(_context(), StepSpec(name="subscriber_dm"), _report()) == ()  # type: ignore[attr-defined]


async def test_a_storage_outage_delivers_nothing_rather_than_failing_the_run() -> None:
    """The wiki half of `wiki+subs` has already published by now."""
    subs = InMemorySubscriptions(fail=True)
    sink = _sink(subs)

    assert await sink.deliver(_context(), StepSpec(name="subscriber_dm"), _report()) == ()  # type: ignore[attr-defined]


# --- inheriting vs overriding (#71, #73) --------------------------------------


async def test_the_scheduler_leaves_inherited_subscriptions_alone() -> None:
    """They are delivered by the channel's action; running them here too
    would send the digest twice, on two different cadences."""
    subs = InMemorySubscriptions()
    await subs.add(_subscription(ALICE, inherited=True))
    scheduler = SubscriptionScheduler(
        subscriptions=subs,
        profiles=InMemoryProfiles(),
        engine=cast("Any", None),  # never reached: inherited rows are skipped
        clock=FakeClock(current=NOW + timedelta(days=1)),
        counters=Counters(),
        capabilities=TELEGRAM_CAPABILITIES,
    )

    assert await scheduler.collect() == []


async def test_a_bare_subscribe_inherits_and_options_override() -> None:
    actions = InMemoryActions()
    await actions.set_actions(GROUP, (parse_action("daily@09:00 summarize"),))
    subs = InMemorySubscriptions()
    service = _service(actions=actions, subscriptions=subs)

    await service.subscribe(GROUP, ALICE, options="")
    await service.subscribe(GROUP, BOB, options="weekly@mon.08:00 stats")

    stored = {sub.dm: sub for sub in await subs.list_all()}
    assert stored[ALICE].inherited is True
    assert stored[BOB].inherited is False
    assert stored[BOB].recipe == "stats"


async def test_inheriting_from_a_channel_with_no_summary_is_refused() -> None:
    """Silently delivering nothing forever is indistinguishable from a
    quiet channel, so say so instead."""
    service = _service(subscriptions=InMemorySubscriptions())

    refused = await service.subscribe(GROUP, ALICE, options="")

    assert refused.text == c.REPLY_SUBS_NOTHING_TO_INHERIT
    assert refused.ok is False


async def test_a_command_only_action_is_not_something_to_inherit() -> None:
    """`/summarize` on demand is not a schedule anyone can subscribe to."""
    actions = InMemoryActions()
    on_demand = parse_action("daily@09:00 summarize")
    await actions.set_actions(
        GROUP,
        (
            type(on_demand)(
                action_id=on_demand.action_id,
                trigger=type(on_demand.trigger)(kind=TriggerKind.COMMAND, command="summarize"),
                source=on_demand.source,
                transforms=on_demand.transforms,
                sinks=on_demand.sinks,
            ),
        ),
    )
    service = _service(actions=actions, subscriptions=InMemorySubscriptions())

    refused = await service.subscribe(GROUP, ALICE, options="")

    assert refused.text == c.REPLY_SUBS_NOTHING_TO_INHERIT


async def test_an_override_needs_no_action_to_exist() -> None:
    service = _service(subscriptions=InMemorySubscriptions())
    created = await service.subscribe(GROUP, ALICE, options="weekly@mon.08:00 stats")
    assert created.ok is True


async def test_a_storage_outage_while_checking_inheritance_says_so() -> None:
    """Not "nothing to inherit": the channel may well have a summary and we
    simply could not look."""
    actions = InMemoryActions()
    actions.fail = True
    service = _service(actions=actions, subscriptions=InMemorySubscriptions())

    refused = await service.subscribe(GROUP, ALICE, options="")

    assert refused.text == c.REPLY_STORAGE_DOWN


def test_a_hand_built_sink_chain_reads_as_the_default_mode() -> None:
    """The gate must never refuse a spec it cannot classify — the
    subscription and log pipelines build their own sinks."""
    odd = replace(parse_action("daily@09:00 summarize"), sinks=(StepSpec(name="reply"),))
    assert delivery_of(odd) == "wiki"


# --- setconsent / subscribable, now neutral (parity) --------------------------


async def test_setconsent_stores_the_policy_and_rejects_anything_else() -> None:
    store = InMemoryProfiles()
    service = _service(store=store)

    ok = await service.set_consent(GROUP, is_admin=True, mode="author_only")
    assert ok.ok is True
    assert store.profiles[GROUP].consent_mode is ConsentMode.AUTHOR_ONLY

    for bad in ("", "confirm", "whatever"):
        refused = await service.set_consent(GROUP, is_admin=True, mode=bad)
        assert refused.ok is False
        assert "immediate | author_only" in refused.text


async def test_setconsent_and_subscribable_are_admin_only() -> None:
    service = _service()
    assert (await service.set_consent(GROUP, is_admin=False, mode="immediate")).text == (
        c.REPLY_NOT_ADMIN
    )
    assert (await service.set_subscribable(GROUP, is_admin=False, enabled=True)).text == (
        c.REPLY_NOT_ADMIN
    )


async def test_subscribable_mints_a_code_and_hands_it_back_for_the_adapter() -> None:
    """What a platform *does* with the code differs — Telegram builds a deep
    link, Discord has none — but minting and storing it does not."""
    store = InMemoryProfiles()
    service = _service(store=store)

    opened = await service.set_subscribable(GROUP, is_admin=True, enabled=True)

    assert opened.payload
    assert store.profiles[GROUP].subscribe_code == opened.payload

    closed = await service.set_subscribable(GROUP, is_admin=True, enabled=False)
    assert closed.payload is None
    assert store.profiles[GROUP].subscribe_code is None


async def test_subscribable_is_refused_without_durable_dms() -> None:
    service = _service(capabilities=IRC_CAPABILITIES)
    refused = await service.set_subscribable(GROUP, is_admin=True, enabled=True)
    assert refused.text == c.REPLY_SUBS_NO_DURABLE_DM


async def test_a_storage_outage_is_reported_not_swallowed() -> None:
    store = InMemoryProfiles()
    store.fail = True
    service = _service(store=store)
    assert (await service.set_consent(GROUP, is_admin=True, mode="immediate")).text == (
        c.REPLY_STORAGE_DOWN
    )
    assert (await service.set_subscribable(GROUP, is_admin=True, enabled=True)).text == (
        c.REPLY_STORAGE_DOWN
    )


async def test_a_v1_deployment_says_self_service_is_off_rather_than_failing() -> None:
    """No profile store at all: the commands are unavailable, not broken."""
    service = CommandService(
        directory=ChannelDirectory(
            store=None,
            default_log_page="Project:Log",
            default_consent=ConsentMode.IMMEDIATE,
            default_repo="",
            page_suffix="logs",
        ),
        groups=GroupPolicy(allowed=set()),
        page_url_for=str,
        counters=Counters(),
        capabilities=TELEGRAM_CAPABILITIES,
    )

    assert (await service.set_consent(GROUP, is_admin=True, mode="immediate")).text == (
        c.REPLY_SELF_SERVICE_OFF
    )
    assert (await service.set_subscribable(GROUP, is_admin=True, enabled=True)).text == (
        c.REPLY_SELF_SERVICE_OFF
    )
