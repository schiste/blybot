"""Rule parsing, description, formatting, and resource selection."""

from __future__ import annotations

import pytest

from blybot.domain.models import DeliveryMode, EventType, RepoEvent, Resource
from blybot.services.rules import (
    RuleParseError,
    describe_rule,
    format_event,
    parse_rule,
    resources_for,
)


def test_parse_bare_trigger_defaults_to_live_and_empty_filter() -> None:
    rule = parse_rule("pr.merged")
    assert rule.trigger is EventType.PR_MERGED
    assert rule.mode is DeliveryMode.LIVE
    assert rule.filter.matches(RepoEvent(EventType.PR_MERGED, "any", "u"))
    assert rule.rule_id  # minted, non-empty


def test_parse_stacks_filters_and_trailing_mode() -> None:
    rule = parse_rule("issue.opened label:bug,urgent author:octocat digest")
    assert rule.trigger is EventType.ISSUE_OPENED
    assert rule.mode is DeliveryMode.DIGEST
    assert rule.filter.labels == frozenset({"bug", "urgent"})
    assert rule.filter.author == "octocat"


def test_parse_repeated_key_unions_like_a_comma_list() -> None:
    rule = parse_rule("issue.opened label:bug label:docs")
    assert rule.filter.labels == frozenset({"bug", "docs"})


def test_parse_title_substring_vs_regex() -> None:
    plain = parse_rule("pr.opened title:hotfix")
    assert plain.filter.title_match == "hotfix"
    assert not plain.filter.title_is_regex
    rx = parse_rule("pr.opened title:/^WIP:/")
    assert rx.filter.title_match == "^WIP:"
    assert rx.filter.title_is_regex


def test_parse_draft_boolean() -> None:
    assert parse_rule("pr.opened draft:false").filter.draft is False
    assert parse_rule("pr.opened draft:true").filter.draft is True


@pytest.mark.parametrize(
    ("text", "match"),
    [
        ("", "event type"),
        ("nope.nope", "Unknown event type"),
        ("issue.opened label", "key:value"),
        ("issue.opened label:", "key:value"),
        ("issue.opened bogus:x", "Unknown filter"),
        ("pr.opened draft:maybe", "draft must be"),
        ("pr.opened title:/[/", "Invalid title regex"),
    ],
)
def test_parse_errors_are_actionable(text: str, match: str) -> None:
    with pytest.raises(RuleParseError, match=match):
        parse_rule(text)


def test_describe_is_reparseable_and_shows_every_condition() -> None:
    rule = parse_rule("pr.merged base:main label:release,hotfix draft:false title:/rc/ digest")
    line = describe_rule(rule)
    assert line.startswith(f"[{rule.rule_id}] pr.merged")
    assert "base:main" in line
    assert "label:hotfix,release" in line  # sorted for stable display
    assert "title:/rc/" in line
    assert "draft:false" in line
    assert "→ digest" in line
    # the descriptive body (minus id and arrow) round-trips through the parser
    body = line.split("] ", 1)[1].replace(" → digest", " digest")
    again = parse_rule(body)
    assert again.filter == rule.filter
    assert again.mode is rule.mode


def test_describe_bare_rule() -> None:
    assert describe_rule(parse_rule("release")).endswith("release → live")


def test_resources_for_collapses_to_needed_streams() -> None:
    rules = [parse_rule("pr.merged"), parse_rule("issue.opened"), parse_rule("release")]
    assert resources_for(rules) == {Resource.PULLS, Resource.ISSUES, Resource.RELEASES}
    assert resources_for([]) == set()


def test_format_event_variants() -> None:
    merged = RepoEvent(EventType.PR_MERGED, "Fix widget", "https://x/pr/9", author="octocat")
    assert format_event(merged) == "PR merged: Fix widget by octocat https://x/pr/9"
    release = RepoEvent(EventType.RELEASE, "v2.0", "https://x/rel/2")
    assert format_event(release) == "Release: v2.0 https://x/rel/2"
