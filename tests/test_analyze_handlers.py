"""AnalysisHandlers tests: admin gate, throttle, acknowledge, outcomes."""

from __future__ import annotations

from datetime import timedelta
from types import SimpleNamespace
from typing import TYPE_CHECKING

from telegram import Update
from telegram.constants import ChatMemberStatus
from telegram.ext import ContextTypes

from blybot.adapters.telegram import analyze as a
from blybot.domain.models import OutboundMessage, Scope
from blybot.domain.ports import ActionError
from blybot.observability import Counters
from blybot.services import analysis_run as ar
from blybot.services.analysis_run import AnalysisService
from blybot.services.engine import ActionEngine
from blybot.services.policy import GroupPolicy, SlidingWindowLimiter
from tests import tg
from tests.fakes import FakeClock, FakeSink, FakeSource, SuffixTransform

if TYPE_CHECKING:
    from unittest.mock import AsyncMock

CONFIRMATION = OutboundMessage(scope=Scope("telegram", str(tg.GROUP.id)), text="Published: url")


def make_handlers(
    source: FakeSource | None = None,
    sink: FakeSink | None = None,
    limit: int = 10,
) -> a.AnalysisHandlers:
    clock = FakeClock()
    engine = ActionEngine(
        sources={"archive_window": source or FakeSource(payload="transcript")},
        transforms={"prompt": SuffixTransform(), "stats": SuffixTransform()},
        sinks={"wiki_section": sink or FakeSink(messages=(CONFIRMATION,))},
        counters=Counters(),
        clock=clock,
    )
    analysis = AnalysisService(
        engine=engine,
        limiter=SlidingWindowLimiter(clock=clock, limit=limit, window=timedelta(hours=1)),
        clock=clock,
        counters=Counters(),
    )
    return a.AnalysisHandlers(analysis=analysis, groups=GroupPolicy(allowed=set()))


def admin_context(
    status: ChatMemberStatus = ChatMemberStatus.ADMINISTRATOR,
    args: list[str] | None = None,
) -> tuple[ContextTypes.DEFAULT_TYPE, AsyncMock]:
    context, bot = tg.make_context(args=args)
    bot.get_chat_member.return_value = SimpleNamespace(status=status)
    return context, bot


def command(text: str) -> Update:
    return tg.command_update(tg.message(text=text, from_user=tg.ALICE))


async def test_summarize_acknowledges_then_posts_the_confirmation() -> None:
    handlers = make_handlers()
    context, bot = admin_context(args=["24h"])

    await handlers.on_summarize(command("/summarize 24h"), context)

    texts = tg.sent_texts(bot)
    assert texts[0] == a.REPLY_WORKING
    assert texts[1] == "Published: url"


async def test_non_admins_are_refused() -> None:
    handlers = make_handlers()
    context, bot = admin_context(status=ChatMemberStatus.MEMBER)

    await handlers.on_talkingpoints(command("/talkingpoints"), context)

    assert "admins" in tg.sent_texts(bot)[0]


async def test_private_chats_are_ignored() -> None:
    handlers = make_handlers()
    context, bot = admin_context()
    update = tg.command_update(tg.message(chat=tg.PRIVATE, text="/stats", from_user=tg.ALICE))

    await handlers.on_stats(update, context)

    bot.send_message.assert_not_awaited()


async def test_analyses_are_throttled_per_chat() -> None:
    handlers = make_handlers(limit=1)
    context, bot = admin_context()

    await handlers.on_stats(command("/stats"), context)
    await handlers.on_stats(command("/stats"), context)

    assert tg.sent_texts(bot).count(ar.REPLY_THROTTLED) == 1


async def test_bad_arguments_reply_with_the_parse_error() -> None:
    handlers = make_handlers()
    context, bot = admin_context(args=["bogus=1"])

    await handlers.on_summarize(command("/summarize bogus=1"), context)

    assert "Expected key=value" in tg.sent_texts(bot)[0]


async def test_run_requires_a_template_and_builds_prompt_recipes() -> None:
    handlers = make_handlers()
    context, bot = admin_context(args=[])
    await handlers.on_run(command("/run"), context)
    assert tg.sent_texts(bot) == [a.REPLY_RUN_USAGE]

    context, bot = admin_context(args=["decision_log", "7d"])
    await handlers.on_run(command("/run decision_log 7d"), context)
    texts = tg.sent_texts(bot)
    assert texts[0] == a.REPLY_WORKING
    assert texts[1] == "Published: url"


async def test_action_errors_reach_the_admin_verbatim() -> None:
    class ExplodingSink:
        async def deliver(self, context: object, _step: object, payload: object) -> tuple[()]:
            del context, payload
            msg = "The analysis failed and nothing was published — try again later."
            raise ActionError(msg)

    handlers = make_handlers()
    handlers.analysis.engine.sinks = {"wiki_section": ExplodingSink()}
    context, bot = admin_context()

    await handlers.on_summarize(command("/summarize"), context)

    assert "nothing was published" in tg.sent_texts(bot)[-1]


async def test_unexpected_failures_reply_generically_and_count() -> None:
    handlers = make_handlers(sink=FakeSink(fail=True))  # RuntimeError from the sink
    context, bot = admin_context()

    await handlers.on_summarize(command("/summarize"), context)

    assert tg.sent_texts(bot)[-1] == ar.REPLY_FAILED
    assert handlers.analysis.counters.snapshot()["analyses_failed"] == 1


async def test_empty_windows_get_a_quiet_notice() -> None:
    handlers = make_handlers(source=FakeSource(payload=None))
    context, bot = admin_context()

    await handlers.on_summarize(command("/summarize"), context)

    assert tg.sent_texts(bot)[-1] == ar.REPLY_EMPTY


async def test_disallowed_groups_get_silence() -> None:
    handlers = make_handlers()
    handlers.groups = GroupPolicy(allowed={"-999"})
    context, bot = admin_context()

    await handlers.on_summarize(command("/summarize"), context)

    bot.send_message.assert_not_awaited()


async def test_run_usage_is_admin_gated_too() -> None:
    handlers = make_handlers()
    context, bot = admin_context(status=ChatMemberStatus.MEMBER, args=[])

    await handlers.on_run(command("/run"), context)

    assert tg.sent_texts(bot) == ["Only this group's admins can configure me."]
