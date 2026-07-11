"""Domain rule model: filter matching semantics and event typing."""

from __future__ import annotations

import pytest

from blybot.domain.models import (
    DeliveryMode,
    EventType,
    RepoEvent,
    Resource,
    Rule,
    RuleFilter,
)


def event(**overrides: object) -> RepoEvent:
    base: dict[str, object] = {
        "event_type": EventType.PR_MERGED,
        "title": "Fix the widget",
        "url": "https://x/pr/1",
        "author": "octocat",
        "labels": frozenset({"bug", "urgent"}),
        "base_branch": "main",
        "draft": False,
        "assignees": frozenset({"maintainer"}),
        "milestone": "v2",
    }
    base.update(overrides)
    return RepoEvent(**base)  # type: ignore[arg-type]


def test_event_type_token_roundtrip_and_resource() -> None:
    assert EventType.from_token("pr.merged") is EventType.PR_MERGED
    assert EventType.PR_MERGED.resource is Resource.PULLS
    assert EventType.COMMENT.resource is Resource.ISSUE_COMMENTS
    with pytest.raises(ValueError, match="unknown event type"):
        EventType.from_token("nope.nope")


def test_empty_filter_matches_anything() -> None:
    assert RuleFilter().matches(event())


def test_label_filter_is_any_of_case_insensitive() -> None:
    assert RuleFilter(labels=frozenset({"bug"})).matches(event())
    assert RuleFilter(labels=frozenset({"BUG"})).matches(event())  # case-insensitive
    assert RuleFilter(labels=frozenset({"docs", "urgent"})).matches(event())  # any-of
    assert not RuleFilter(labels=frozenset({"docs"})).matches(event())


def test_distinct_keys_and_together() -> None:
    both = RuleFilter(labels=frozenset({"bug"}), base="main", author="octocat")
    assert both.matches(event())
    assert not both.matches(event(base_branch="release"))  # one key fails → no match


def test_title_substring_and_regex() -> None:
    assert RuleFilter(title_match="widget").matches(event())  # substring, case-insensitive
    assert RuleFilter(title_match="WIDGET").matches(event())
    assert RuleFilter(title_match=r"fix .*widget", title_is_regex=True).matches(event())
    assert not RuleFilter(title_match="gadget").matches(event())


def test_draft_and_assignee_and_milestone() -> None:
    assert RuleFilter(draft=False).matches(event())
    assert not RuleFilter(draft=True).matches(event())
    assert RuleFilter(assignee="maintainer").matches(event())
    assert not RuleFilter(assignee="someone").matches(event())
    assert RuleFilter(milestone="V2").matches(event())  # case-insensitive
    assert not RuleFilter(milestone="v3").matches(event())


def test_rule_matches_requires_trigger_and_filter() -> None:
    rule = Rule(
        rule_id="r1",
        trigger=EventType.PR_MERGED,
        filter=RuleFilter(labels=frozenset({"urgent"})),
        mode=DeliveryMode.DIGEST,
    )
    assert rule.matches(event())
    assert not rule.matches(event(event_type=EventType.ISSUE_OPENED))  # wrong trigger
    assert not rule.matches(event(labels=frozenset({"bug"})))  # filter fails
