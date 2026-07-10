"""GitHub issue tracker adapter and feedback composition tests."""

from __future__ import annotations

import json

import httpx
import pytest

from blybot.adapters.github.issues import GitHubIssueTracker
from blybot.domain.ports import IssueTrackerError
from blybot.services.feedback import FeedbackService


class FakeGitHub:
    def __init__(self, status: int = 201) -> None:
        self.status = status
        self.requests: list[dict[str, str]] = []
        self.fail_transport = False

    def handler(self, request: httpx.Request) -> httpx.Response:
        if self.fail_transport:
            raise httpx.ConnectError("down")
        payload = json.loads(request.content.decode())
        self.requests.append(payload)
        return httpx.Response(
            self.status, json={"html_url": "https://github.com/schiste/blybot/issues/42"}
        )


def make_tracker(github: FakeGitHub) -> GitHubIssueTracker:
    return GitHubIssueTracker(
        repo="schiste/blybot",
        token="ghp_dummy",  # noqa: S106 -- test fixture, not a credential
        user_agent="Blybot/0.1 (test)",
        transport=httpx.MockTransport(github.handler),
    )


async def test_open_issue_posts_and_returns_the_url() -> None:
    github = FakeGitHub()
    tracker = make_tracker(github)
    url = await tracker.open_issue("title", "body")
    assert url == "https://github.com/schiste/blybot/issues/42"
    assert github.requests == [{"title": "title", "body": "body"}]
    await tracker.aclose()


async def test_non_201_raises() -> None:
    github = FakeGitHub(status=403)
    tracker = make_tracker(github)
    with pytest.raises(IssueTrackerError, match="403"):
        await tracker.open_issue("t", "b")
    await tracker.aclose()


async def test_transport_failure_raises() -> None:
    github = FakeGitHub()
    github.fail_transport = True
    tracker = make_tracker(github)
    with pytest.raises(IssueTrackerError, match="reach"):
        await tracker.open_issue("t", "b")
    await tracker.aclose()


async def test_report_indents_the_text_so_it_cannot_ping_or_render() -> None:
    github = FakeGitHub()
    tracker = make_tracker(github)
    await FeedbackService(tracker).report("@someone look\n**bold** [link](x)")

    (issue,) = github.requests
    body_lines = issue["body"].splitlines()
    assert body_lines[-2] == "    @someone look"
    assert body_lines[-1] == "    **bold** [link](x)"
    assert "anonymously" in issue["body"]
    await tracker.aclose()


async def test_report_title_is_the_collapsed_first_words_capped() -> None:
    github = FakeGitHub()
    tracker = make_tracker(github)
    long_text = "word " * 40
    await FeedbackService(tracker).report(long_text)

    (issue,) = github.requests
    assert len(issue["title"]) <= 64
    assert issue["title"].endswith("…")
    await tracker.aclose()
