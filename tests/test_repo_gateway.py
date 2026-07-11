"""GitHubRepoGateway tests against a scripted GitHub API."""

from __future__ import annotations

import json
from typing import Any, Protocol

import httpx
import pytest

from blybot.adapters.github.gateway import GitHubRepoGateway
from blybot.domain.models import EventType, Resource
from blybot.domain.ports import IssueTrackerError


class _Api(Protocol):
    """Any scripted API: exposes a MockTransport-compatible handler."""

    def handler(self, request: httpx.Request) -> httpx.Response: ...


class FakeRepoApi:
    def __init__(self) -> None:
        self.permissions: dict[str, Any] = {"push": True}
        self.repo_status = 200
        self.issues_status = 200
        self.fail_transport = False
        self.seen_tokens: list[str] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        if self.fail_transport:
            raise httpx.ConnectError("down")
        self.seen_tokens.append(request.headers.get("Authorization", ""))
        path = request.url.path
        if request.method == "POST" and path.endswith("/issues"):
            payload = json.loads(request.content.decode())
            return httpx.Response(
                201, json={"html_url": f"https://github.com/x/y/issues/7#{payload['title']}"}
            )
        if path.endswith("/issues"):
            return httpx.Response(
                self.issues_status,
                json=[{"title": "First"}, {"title": "Second"}, {"title": "Third"}],
            )
        return httpx.Response(
            self.repo_status,
            json={"permissions": self.permissions, "open_issues_count": 12},
        )


def make_gateway(api: _Api) -> GitHubRepoGateway:
    return GitHubRepoGateway(
        user_agent="Blybot/0.1 (test)", transport=httpx.MockTransport(api.handler)
    )


async def test_validate_accepts_push_or_triage_tokens() -> None:
    api = FakeRepoApi()
    gateway = make_gateway(api)
    assert await gateway.validate_token("x/y", "tok")
    api.permissions = {"push": False, "triage": True}
    assert await gateway.validate_token("x/y", "tok")
    api.permissions = {"pull": True}
    assert not await gateway.validate_token("x/y", "tok")
    await gateway.aclose()


async def test_validate_rejects_unreachable_or_invisible_repos() -> None:
    api = FakeRepoApi()
    gateway = make_gateway(api)
    api.repo_status = 404
    assert not await gateway.validate_token("x/y", "tok")
    api.fail_transport = True
    assert not await gateway.validate_token("x/y", "tok")
    await gateway.aclose()


async def test_open_issue_uses_the_group_token_per_call() -> None:
    api = FakeRepoApi()
    gateway = make_gateway(api)
    url = await gateway.open_issue("x/y", "group-token", "title", "body")
    assert url.startswith("https://github.com/x/y/issues/7")
    assert api.seen_tokens == ["Bearer group-token"]
    await gateway.aclose()


async def test_open_issue_failures_raise() -> None:
    api = FakeRepoApi()
    gateway = make_gateway(api)
    api.fail_transport = True
    with pytest.raises(IssueTrackerError, match="reach"):
        await gateway.open_issue("x/y", "tok", "t", "b")
    await gateway.aclose()

    class Forbidden(FakeRepoApi):
        def handler(self, request: httpx.Request) -> httpx.Response:
            del request
            return httpx.Response(403, json={})

    gateway = make_gateway(Forbidden())
    with pytest.raises(IssueTrackerError, match="403"):
        await gateway.open_issue("x/y", "tok", "t", "b")
    await gateway.aclose()


async def test_summary_combines_counts_and_recent_titles() -> None:
    gateway = make_gateway(FakeRepoApi())
    summary = await gateway.open_summary("x/y", "tok")
    assert summary.repo == "x/y"
    assert summary.open_count == 12
    assert summary.recent_titles == ("First", "Second", "Third")
    await gateway.aclose()


async def test_summary_failures_raise() -> None:
    api = FakeRepoApi()
    gateway = make_gateway(api)
    api.issues_status = 500
    with pytest.raises(IssueTrackerError, match="unavailable"):
        await gateway.open_summary("x/y", "tok")
    api.fail_transport = True
    with pytest.raises(IssueTrackerError, match="reach"):
        await gateway.open_summary("x/y", "tok")
    await gateway.aclose()


WM = "2026-07-01T00:00:00Z"


class PollApi:
    """Serves per-resource JSON payloads keyed by URL path suffix."""

    def __init__(self, payloads: dict[str, Any]) -> None:
        self.payloads = payloads
        self.status = 200
        self.fail_transport = False
        self.seen_since: dict[str, str | None] = {}

    def handler(self, request: httpx.Request) -> httpx.Response:
        if self.fail_transport:
            raise httpx.ConnectError("down")
        path = request.url.path
        self.seen_since[path] = request.url.params.get("since")
        if self.status != 200:
            return httpx.Response(self.status, json=[])
        # Longest suffix first so /issues/comments beats /issues.
        for suffix in sorted(self.payloads, key=len, reverse=True):
            if path.endswith(suffix):
                return httpx.Response(200, json=self.payloads[suffix])
        return httpx.Response(200, json=[])


ISSUES = [
    {
        "title": "Bug",
        "html_url": "https://x/i/1",
        "state": "open",
        "created_at": "2026-07-05T00:00:00Z",
        "updated_at": "2026-07-05T00:00:00Z",
        "closed_at": None,
        "user": {"login": "alice"},
        "labels": [{"name": "bug"}, {"name": "urgent"}],
        "assignees": [{"login": "bob"}],
        "milestone": {"title": "v2"},
    },
    {
        "title": "Done",
        "html_url": "https://x/i/2",
        "state": "closed",
        "created_at": "2026-06-01T00:00:00Z",
        "updated_at": "2026-07-06T00:00:00Z",
        "closed_at": "2026-07-06T00:00:00Z",
        "user": {"login": "carol"},
        "labels": [],
    },
    {  # a PR surfaced by the /issues list — must be skipped
        "title": "A PR",
        "html_url": "https://x/i/3",
        "pull_request": {"url": "..."},
        "created_at": "2026-07-05T00:00:00Z",
        "updated_at": "2026-07-05T00:00:00Z",
    },
]


async def test_poll_issues_splits_prs_detects_open_close_and_enriches() -> None:
    api = PollApi({"/issues": ISSUES})
    gateway = make_gateway(api)
    events, cursor = await gateway.poll_resource("x/y", "tok", Resource.ISSUES, WM)
    assert [event.event_type for event in events] == [
        EventType.ISSUE_OPENED,  # oldest transition first
        EventType.ISSUE_CLOSED,
    ]
    opened = events[0]
    assert opened.title == "Bug"
    assert opened.author == "alice"
    assert opened.labels == frozenset({"bug", "urgent"})
    assert opened.assignees == frozenset({"bob"})
    assert opened.milestone == "v2"
    assert cursor == "2026-07-06T00:00:00Z"  # newest updated_at
    assert api.seen_since["/repos/x/y/issues"] == WM  # watermark forwarded as since
    await gateway.aclose()


async def test_poll_baseline_emits_nothing_but_advances_cursor() -> None:
    api = PollApi({"/issues": ISSUES})
    gateway = make_gateway(api)
    events, cursor = await gateway.poll_resource("x/y", "tok", Resource.ISSUES, None)
    assert events == []
    assert cursor == "2026-07-06T00:00:00Z"
    assert api.seen_since["/repos/x/y/issues"] is None  # no since on a baseline poll
    await gateway.aclose()


PULLS = [
    {
        "title": "Feature",
        "html_url": "https://x/p/1",
        "state": "open",
        "created_at": "2026-07-05T00:00:00Z",
        "updated_at": "2026-07-05T00:00:00Z",
        "closed_at": None,
        "merged_at": None,
        "user": {"login": "dev"},
        "base": {"ref": "main"},
        "draft": True,
    },
    {
        "title": "Shipped",
        "html_url": "https://x/p/2",
        "state": "closed",
        "created_at": "2026-06-01T00:00:00Z",
        "updated_at": "2026-07-06T00:00:00Z",
        "closed_at": "2026-07-06T00:00:00Z",
        "merged_at": "2026-07-06T00:00:00Z",
        "user": {"login": "dev"},
        "base": {"ref": "main"},
        "draft": False,
    },
    {
        "title": "Rejected",
        "html_url": "https://x/p/3",
        "state": "closed",
        "created_at": "2026-06-02T00:00:00Z",
        "updated_at": "2026-07-06T12:00:00Z",
        "closed_at": "2026-07-06T12:00:00Z",
        "merged_at": None,
        "user": {"login": "dev"},
        "base": {"ref": "dev"},
        "draft": False,
    },
]


async def test_poll_pulls_detects_open_merged_closed_with_base_and_draft() -> None:
    gateway = make_gateway(PollApi({"/pulls": PULLS}))
    events, cursor = await gateway.poll_resource("x/y", "tok", Resource.PULLS, WM)
    assert [event.event_type for event in events] == [
        EventType.PR_OPENED,
        EventType.PR_MERGED,  # merged wins over closed for the same PR
        EventType.PR_CLOSED,
    ]
    assert events[0].draft is True
    assert events[0].base_branch == "main"
    assert events[2].base_branch == "dev"
    assert cursor == "2026-07-06T12:00:00Z"
    await gateway.aclose()


COMMENTS = [
    {
        "body": "  looks good  ",
        "html_url": "https://x/c/1",
        "user": {"login": "al"},
        "created_at": "2026-07-05T00:00:00Z",
        "updated_at": "2026-07-05T00:00:00Z",
    },
    {  # edited old comment: created before the watermark → not a new comment
        "body": "old but edited",
        "html_url": "https://x/c/2",
        "user": {"login": "bo"},
        "created_at": "2026-06-01T00:00:00Z",
        "updated_at": "2026-07-07T00:00:00Z",
    },
]


async def test_poll_comments_only_new_ones_fire() -> None:
    gateway = make_gateway(PollApi({"/issues/comments": COMMENTS}))
    events, cursor = await gateway.poll_resource("x/y", "tok", Resource.ISSUE_COMMENTS, WM)
    assert [event.event_type for event in events] == [EventType.COMMENT]
    assert events[0].title == "looks good"  # body trimmed and clipped
    assert cursor == "2026-07-07T00:00:00Z"  # watermark still advances past edits
    await gateway.aclose()


RELEASES = [
    {
        "name": "v2.0",
        "html_url": "https://x/r/1",
        "published_at": "2026-07-05T00:00:00Z",
        "author": {"login": "rel"},
    },
    {"name": "wip", "html_url": "https://x/r/2", "published_at": None},  # draft: skipped
]


async def test_poll_releases_skips_drafts() -> None:
    gateway = make_gateway(PollApi({"/releases": RELEASES}))
    events, cursor = await gateway.poll_resource("x/y", "tok", Resource.RELEASES, WM)
    assert [event.event_type for event in events] == [EventType.RELEASE]
    assert events[0].title == "v2.0"
    assert events[0].author == "rel"
    assert cursor == "2026-07-05T00:00:00Z"
    await gateway.aclose()


async def test_poll_transport_and_http_and_malformed_failures() -> None:
    api = PollApi({"/pulls": PULLS})
    gateway = make_gateway(api)
    api.fail_transport = True
    with pytest.raises(IssueTrackerError, match="reach"):
        await gateway.poll_resource("x/y", "tok", Resource.PULLS, WM)
    await gateway.aclose()

    api = PollApi({"/pulls": PULLS})
    api.status = 500
    gateway = make_gateway(api)
    with pytest.raises(IssueTrackerError, match="HTTP 500"):
        await gateway.poll_resource("x/y", "tok", Resource.PULLS, WM)
    await gateway.aclose()

    gateway = make_gateway(PollApi({"/pulls": [123]}))  # not a dict
    with pytest.raises(IssueTrackerError, match="malformed pulls"):
        await gateway.poll_resource("x/y", "tok", Resource.PULLS, WM)
    await gateway.aclose()


async def test_poll_tolerates_items_missing_created_at() -> None:
    """A null created_at skips the 'opened' event but still detects closure."""
    issue = {
        "title": "Ghost",
        "html_url": "u",
        "state": "closed",
        "created_at": None,
        "updated_at": "2026-07-09T00:00:00Z",
        "closed_at": "2026-07-09T00:00:00Z",
    }
    gateway = make_gateway(PollApi({"/issues": [issue]}))
    events, _ = await gateway.poll_resource("x/y", "tok", Resource.ISSUES, WM)
    assert [event.event_type for event in events] == [EventType.ISSUE_CLOSED]
    await gateway.aclose()

    pull = {**issue, "merged_at": None, "base": {"ref": "main"}}
    gateway = make_gateway(PollApi({"/pulls": [pull]}))
    events, _ = await gateway.poll_resource("x/y", "tok", Resource.PULLS, WM)
    assert [event.event_type for event in events] == [EventType.PR_CLOSED]
    await gateway.aclose()

    comment = {"html_url": "u", "user": {"login": "z"}, "created_at": None, "updated_at": WM}
    gateway = make_gateway(PollApi({"/issues/comments": [comment]}))
    events, _ = await gateway.poll_resource("x/y", "tok", Resource.ISSUE_COMMENTS, WM)
    assert events == []
    await gateway.aclose()
