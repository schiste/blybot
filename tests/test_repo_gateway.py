"""GitHubRepoGateway tests against a scripted GitHub API."""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from blybot.adapters.github.gateway import GitHubRepoGateway
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
