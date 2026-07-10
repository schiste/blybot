"""GitHubRepoGateway tests against a scripted GitHub API."""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from blybot.adapters.github.gateway import GitHubRepoGateway
from blybot.domain.models import EventKind
from blybot.domain.ports import IssueTrackerError


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


def make_gateway(api: FakeRepoApi) -> GitHubRepoGateway:
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


RAW_EVENTS = [
    {
        "id": "30",
        "type": "ReleaseEvent",
        "payload": {
            "action": "published",
            "release": {"name": "v2", "html_url": "https://x/rel/2"},
        },
    },
    {
        "id": "20",
        "type": "PullRequestEvent",
        "payload": {
            "action": "closed",
            "pull_request": {"merged": True, "title": "Fix it", "html_url": "https://x/pr/9"},
        },
    },
    {
        "id": "15",
        "type": "PullRequestEvent",
        "payload": {
            "action": "closed",
            "pull_request": {"merged": False, "title": "Abandoned", "html_url": "https://x/pr/8"},
        },
    },
    {
        "id": "12",
        "type": "IssuesEvent",
        "payload": {"action": "opened", "issue": {"title": "Bug", "html_url": "https://x/i/7"}},
    },
    {"id": "11", "type": "PushEvent", "payload": {}},  # unmapped kind: dropped
]


class EventsApi(FakeRepoApi):
    def __init__(self) -> None:
        super().__init__()
        self.not_modified = False
        self.seen_etags: list[str | None] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        if self.fail_transport:
            raise httpx.ConnectError("down")
        if request.url.path.endswith("/events"):
            self.seen_etags.append(request.headers.get("If-None-Match"))
            if self.not_modified:
                return httpx.Response(304)
            return httpx.Response(200, json=RAW_EVENTS, headers={"ETag": 'W/"fresh"'})
        return super().handler(request)


async def test_events_baseline_never_replays_history() -> None:
    api = EventsApi()
    gateway = make_gateway(api)
    events, cursor = await gateway.events_since("x/y", "tok", None)
    assert events == []
    assert cursor == 'W/"fresh"|30'
    await gateway.aclose()


async def test_events_filter_by_watermark_map_kinds_and_order_oldest_first() -> None:
    gateway = make_gateway(EventsApi())
    events, cursor = await gateway.events_since("x/y", "tok", 'W/"old"|10')
    kinds = [event.kind for event in events]
    assert kinds == [EventKind.ISSUES, EventKind.PRS, EventKind.RELEASES]  # oldest first
    assert events[0].title == "New issue: Bug"
    assert events[1].title == "Merged: Fix it"  # the unmerged PR was dropped
    assert events[2].url == "https://x/rel/2"
    assert cursor == 'W/"fresh"|30'
    await gateway.aclose()


async def test_events_304_is_a_free_poll() -> None:
    api = EventsApi()
    api.not_modified = True
    gateway = make_gateway(api)
    events, cursor = await gateway.events_since("x/y", "tok", 'W/"old"|12')
    assert (events, cursor) == ([], 'W/"old"|12')
    assert api.seen_etags == ['W/"old"']
    await gateway.aclose()


async def test_events_failures_raise() -> None:
    api = EventsApi()
    gateway = make_gateway(api)
    api.fail_transport = True
    with pytest.raises(IssueTrackerError, match="reach"):
        await gateway.events_since("x/y", "tok", None)
    await gateway.aclose()

    class Down(EventsApi):
        def handler(self, request: httpx.Request) -> httpx.Response:
            del request
            return httpx.Response(500)

    gateway = make_gateway(Down())
    with pytest.raises(IssueTrackerError, match="500"):
        await gateway.events_since("x/y", "tok", None)
    await gateway.aclose()
