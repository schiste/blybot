"""Payload guards for the re-homed pipeline components (v3 phase 5)."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from blybot.domain.models import ActionContext, LogContent, Scope, StepSpec
from blybot.domain.ports import ActionError
from blybot.observability import Counters
from blybot.services.actions import command_action
from blybot.services.feedback import FeedbackService
from blybot.services.notify import ChatMessagesSink, RuleMatchTransform
from blybot.services.publish import ChatConfirmSink, LogPublishTransform
from tests.test_private_handlers import FakeTracker

NOW = datetime(2026, 7, 25, 12, 0, tzinfo=UTC)


def context() -> ActionContext:
    return ActionContext(
        scope=Scope("telegram", "-1"),
        spec=command_action("summarize", "summarize", []),
        now=NOW,
    )


async def test_issue_tracker_sink_rejects_non_text_payloads() -> None:
    sink = FeedbackService(tracker=FakeTracker())
    for bad in (42, "", "   "):
        with pytest.raises(ActionError, match="report text"):
            await sink.deliver(context(), bad)


async def test_rule_match_rejects_foreign_payloads() -> None:
    transform = RuleMatchTransform(counters=Counters())
    with pytest.raises(ActionError, match="repo events batch"):
        await transform.apply(context(), context().spec.transforms[0], "not a batch")


async def test_chat_messages_sink_rejects_non_line_payloads() -> None:
    sink = ChatMessagesSink()
    for bad in ("a bare string", (1, 2), ["list", "not", "tuple"]):
        with pytest.raises(ActionError, match="message lines"):
            await sink.deliver(context(), bad)


async def test_log_publish_rejects_foreign_payloads_and_missing_pages() -> None:
    transform = LogPublishTransform(
        service=None,  # type: ignore[arg-type]  # guards fire before any use
        page_url_for=lambda title: title,
    )
    with pytest.raises(ActionError, match="message's content"):
        await transform.apply(context(), context().spec.transforms[0], "not log content")

    with pytest.raises(ActionError, match="page parameter"):
        await transform.apply(context(), StepSpec(name="log_publish"), LogContent(text="hi"))


async def test_chat_confirm_rejects_foreign_payloads() -> None:
    sink = ChatConfirmSink()
    with pytest.raises(ActionError, match="published log"):
        await sink.deliver(context(), "not a published log")
