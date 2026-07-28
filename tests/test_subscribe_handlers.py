"""SubscriptionHandlers DM-command tests (redeem, subscribe, list, unsubscribe)."""

from __future__ import annotations

from dataclasses import replace

import pytest
from telegram import Update

from blybot.adapters.telegram import subscribe as sub
from blybot.adapters.telegram.subscribe import SubscriptionHandlers
from blybot.adapters.telegram.transport import TELEGRAM_CAPABILITIES
from blybot.domain.models import GroupProfile, PlatformCapabilities, Schedule, Scope
from blybot.domain.subscriptions import Subscription
from blybot.services.subscriptions import SubscriptionBinding
from tests import tg
from tests.fakes import FakeClock, InMemoryProfiles, InMemorySubscriptions


def gscope(chat_id: int, thread_id: int = 0) -> Scope:
    return Scope("telegram", str(chat_id), str(thread_id) if thread_id else "")


def dmscope(chat_id: int) -> Scope:
    return Scope("telegram", str(chat_id))


def make(
    capabilities: PlatformCapabilities = TELEGRAM_CAPABILITIES,
) -> tuple[SubscriptionHandlers, InMemoryProfiles, InMemorySubscriptions, SubscriptionBinding]:
    profiles = InMemoryProfiles()
    subs = InMemorySubscriptions()
    binding = SubscriptionBinding(clock=FakeClock())
    handlers = SubscriptionHandlers(
        profiles=profiles,
        subscriptions=subs,
        binding=binding,
        default_lang="en",
        capabilities=capabilities,
    )
    return handlers, profiles, subs, binding


async def test_subscribe_refused_without_durable_dm() -> None:
    handlers, _profiles, subs, binding = make(
        capabilities=replace(TELEGRAM_CAPABILITIES, durable_dm=False)
    )
    binding.open_entry(dmscope(777), gscope(-100))
    context, bot = tg.make_context()
    await handlers.on_subscribe(dm(), context)
    assert tg.sent_texts(bot) == [sub.REPLY_SUBS_UNAVAILABLE]
    assert subs.subs == {}  # admission gated: nothing created


def dm(text: str = "/x") -> Update:
    return tg.command_update(tg.message(chat=tg.PRIVATE, text=text))


async def test_redeem_valid_code_arms_and_prompts() -> None:
    handlers, profiles, _subs, binding = make()
    await profiles.upsert(GroupProfile(scope=gscope(-100), subscribe_code="code123"))
    context, bot = tg.make_context()
    await handlers.redeem_link(context, dmscope(777), "code123")
    assert binding.pending_target(dmscope(777)) == gscope(-100)
    assert "subscribing" in tg.sent_texts(bot)[0].lower()


async def test_redeem_invalid_code_is_refused() -> None:
    handlers, _profiles, _subs, _binding = make()
    context, bot = tg.make_context()
    await handlers.redeem_link(context, dmscope(777), "nope")
    assert tg.sent_texts(bot) == [sub.REPLY_LINK_INVALID]


async def test_redeem_reports_storage_outage() -> None:
    handlers, profiles, _subs, _binding = make()
    profiles.fail = True
    context, bot = tg.make_context()
    await handlers.redeem_link(context, dmscope(777), "code123")
    assert tg.sent_texts(bot) == [sub.REPLY_STORAGE_DOWN]


async def test_subscribe_without_a_tapped_link_explains() -> None:
    handlers, _profiles, _subs, _binding = make()
    context, bot = tg.make_context()
    await handlers.on_subscribe(dm(), context)
    assert tg.sent_texts(bot) == [sub.REPLY_NO_PENDING]


async def test_subscribe_creates_the_subscription() -> None:
    handlers, _profiles, subs, binding = make()
    binding.open_entry(dmscope(777), gscope(-100))
    context, bot = tg.make_context(args=["daily@09:00", "stats"])
    await handlers.on_subscribe(dm(), context)
    (created,) = list(subs.subs.values())
    assert (created.dm.channel, created.scope.channel, created.recipe) == ("777", "-100", "stats")
    assert created.schedule.token == "daily@09:00"  # noqa: S105 -- schedule token
    assert binding.pending_target(dmscope(777)) is None  # consumed
    assert tg.sent_texts(bot)[0].startswith("Subscribed [")


async def test_subscribe_rejects_bad_options() -> None:
    handlers, _profiles, subs, binding = make()
    binding.open_entry(dmscope(777), gscope(-100))
    context, bot = tg.make_context(args=["gibberish@nope"])
    await handlers.on_subscribe(dm(), context)
    assert subs.subs == {}
    assert "Didn't understand" in tg.sent_texts(bot)[0]


async def test_subscribe_reports_storage_outage() -> None:
    handlers, _profiles, subs, binding = make()
    binding.open_entry(dmscope(777), gscope(-100))
    subs.fail = True
    context, bot = tg.make_context()
    await handlers.on_subscribe(dm(), context)
    assert tg.sent_texts(bot) == [sub.REPLY_STORAGE_DOWN]


async def test_unsubscribe_usage_removes_and_reports_missing() -> None:
    handlers, _profiles, subs, _binding = make()
    context, bot = tg.make_context(args=[])
    await handlers.on_unsubscribe(dm(), context)
    assert tg.sent_texts(bot) == [sub.REPLY_UNSUB_USAGE]

    await subs.add(_a_sub("s1", dm=tg.PRIVATE.id))
    context, bot = tg.make_context(args=["s1"])
    await handlers.on_unsubscribe(dm(), context)
    assert tg.sent_texts(bot) == [sub.REPLY_UNSUBSCRIBED]
    assert subs.subs == {}

    context, bot = tg.make_context(args=["ghost"])
    await handlers.on_unsubscribe(dm(), context)
    assert tg.sent_texts(bot) == [sub.REPLY_NO_SUCH_SUB]


async def test_unsubscribe_reports_storage_outage() -> None:
    handlers, _profiles, subs, _binding = make()
    subs.fail = True
    context, bot = tg.make_context(args=["s1"])
    await handlers.on_unsubscribe(dm(), context)
    assert tg.sent_texts(bot) == [sub.REPLY_STORAGE_DOWN]


async def test_mysubs_lists_or_says_none() -> None:
    handlers, _profiles, subs, _binding = make()
    context, bot = tg.make_context()
    await handlers.on_mysubs(dm(), context)
    assert tg.sent_texts(bot) == [sub.REPLY_NO_SUBS]

    await subs.add(_a_sub("s1", dm=tg.PRIVATE.id))
    context, bot = tg.make_context()
    await handlers.on_mysubs(dm(), context)
    listing = tg.sent_texts(bot)[0]
    assert sub.REPLY_SUBS_HEADER in listing
    assert "[s1] daily@08:00 summarize (en)" in listing


async def test_mysubs_reports_storage_outage() -> None:
    handlers, _profiles, subs, _binding = make()
    subs.fail = True
    context, bot = tg.make_context()
    await handlers.on_mysubs(dm(), context)
    assert tg.sent_texts(bot) == [sub.REPLY_STORAGE_DOWN]


@pytest.mark.parametrize("method", ["on_subscribe", "on_unsubscribe", "on_mysubs"])
async def test_commands_ignore_updates_without_a_chat(method: str) -> None:
    handlers, _profiles, _subs, _binding = make()
    context, bot = tg.make_context()
    await getattr(handlers, method)(Update(update_id=1), context)  # no message → no chat
    assert tg.sent_texts(bot) == []


def _a_sub(sub_id: str, dm: int) -> Subscription:
    return Subscription(
        sub_id=sub_id,
        dm=dmscope(dm),
        scope=gscope(-100),
        schedule=Schedule(kind="daily", hour=8),
        recipe="summarize",
        lang="en",
    )
