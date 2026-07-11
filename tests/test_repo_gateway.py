"""GitHubRepoGateway tests against a scripted GitHub API."""

from __future__ import annotations

import json
import logging
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
    """Serves per-resource JSON keyed by URL path suffix.

    A ``payloads`` value is a flat list returned on page 1 (empty
    after). A ``pages`` value is a list of per-page lists, indexed by the
    ``page`` query param, to exercise pagination. ``date`` sets the
    ``Date`` response header (the baseline watermark source).
    """

    def __init__(
        self,
        payloads: dict[str, Any] | None = None,
        *,
        pages: dict[str, list[list[Any]]] | None = None,
        date: str | None = None,
    ) -> None:
        self.payloads = payloads or {}
        self.pages = pages or {}
        self.date = date
        self.status = 200
        self.fail_transport = False
        self.seen_since: dict[str, str | None] = {}
        self.seen_pages: list[int] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        if self.fail_transport:
            raise httpx.ConnectError("down")
        path = request.url.path
        page = int(request.url.params.get("page", "1"))
        self.seen_since[path] = request.url.params.get("since")
        self.seen_pages.append(page)
        headers = {"Date": self.date} if self.date else {}
        if self.status != 200:
            return httpx.Response(self.status, json=[], headers=headers)
        # Longest suffix first so /issues/comments beats /issues.
        for suffix in sorted({**self.payloads, **self.pages}, key=len, reverse=True):
            if path.endswith(suffix):
                if suffix in self.pages:
                    batches = self.pages[suffix]
                    data = batches[page - 1] if 1 <= page <= len(batches) else []
                else:
                    data = self.payloads[suffix] if page == 1 else []
                return httpx.Response(200, json=data, headers=headers)
        return httpx.Response(200, json=[], headers=headers)


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


async def test_poll_baseline_stamps_server_now_and_emits_nothing() -> None:
    api = PollApi({"/issues": ISSUES}, date="Wed, 08 Jul 2026 09:30:00 GMT")
    gateway = make_gateway(api)
    events, cursor = await gateway.poll_resource("x/y", "tok", Resource.ISSUES, None)
    assert events == []
    assert cursor == "2026-07-08T09:30:00Z"  # server "now", not max item time
    assert api.seen_since["/repos/x/y/issues"] is None  # no since on a baseline poll
    await gateway.aclose()


async def test_poll_baseline_then_first_event_on_a_previously_empty_stream_fires() -> None:
    # Stream empty at enable → baseline stamps now; the first release after
    # that (published later) must still fire (regression guard).
    api = PollApi({"/releases": []}, date="Wed, 08 Jul 2026 00:00:00 GMT")
    gateway = make_gateway(api)
    _events, baseline = await gateway.poll_resource("x/y", "tok", Resource.RELEASES, None)
    assert baseline == "2026-07-08T00:00:00Z"

    api.payloads["/releases"] = [
        {"name": "v1", "html_url": "https://x/r/1", "published_at": "2026-07-09T00:00:00Z"}
    ]
    events, _cursor = await gateway.poll_resource("x/y", "tok", Resource.RELEASES, baseline)
    assert [event.title for event in events] == ["v1"]
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

    gateway = make_gateway(PollApi({"/pulls": [123]}))  # not a dict (caught in _fetch_pages)
    with pytest.raises(IssueTrackerError, match="malformed pulls"):
        await gateway.poll_resource("x/y", "tok", Resource.PULLS, WM)
    await gateway.aclose()

    # A since-based stream doesn't inspect items while paging, so a
    # malformed item surfaces later in _collect instead.
    gateway = make_gateway(PollApi({"/issues": [123]}))
    with pytest.raises(IssueTrackerError, match="malformed issues"):
        await gateway.poll_resource("x/y", "tok", Resource.ISSUES, WM)
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


def _issue(n: int, updated: str) -> dict[str, Any]:
    return {
        "title": f"i{n}",
        "html_url": f"https://x/i/{n}",
        "state": "open",
        "created_at": "2026-07-05T00:00:00Z",  # > WM: fires ISSUE_OPENED
        "updated_at": updated,
        "closed_at": None,
    }


async def test_poll_paginates_a_since_stream_until_a_short_page() -> None:
    full = [_issue(n, f"2026-07-05T00:00:{n:02d}Z") for n in range(50)]
    tail = [_issue(99, "2026-07-05T00:01:00Z")]
    api = PollApi(pages={"/issues": [full, tail]})
    gateway = make_gateway(api)
    events, cursor = await gateway.poll_resource("x/y", "tok", Resource.ISSUES, WM)
    assert len(events) == 51  # every opened event across both pages
    assert api.seen_pages == [1, 2]  # page 2 fetched because page 1 was full
    assert cursor == "2026-07-05T00:01:00Z"
    await gateway.aclose()


async def test_poll_pulls_stops_once_a_page_ends_at_the_watermark() -> None:
    prs = []
    for n in range(50):  # desc by updated; the last one is already <= WM
        updated = "2026-07-05T00:00:00Z" if n < 49 else "2026-06-30T00:00:00Z"
        created = "2026-07-05T00:00:00Z" if n < 49 else "2026-06-01T00:00:00Z"
        prs.append(
            {
                "title": f"p{n}",
                "html_url": f"https://x/p/{n}",
                "state": "open",
                "created_at": created,
                "updated_at": updated,
                "closed_at": None,
                "merged_at": None,
                "base": {"ref": "main"},
            }
        )
    api = PollApi(pages={"/pulls": [prs, [{"title": "extra"}]]})
    gateway = make_gateway(api)
    events, _cursor = await gateway.poll_resource("x/y", "tok", Resource.PULLS, WM)
    assert api.seen_pages == [1]  # early-stopped at the watermark; page 2 untouched
    assert all(event.event_type is EventType.PR_OPENED for event in events)
    await gateway.aclose()


async def test_poll_saturation_is_logged_as_a_possible_gap(
    caplog: pytest.LogCaptureFixture,
) -> None:
    page = [
        {"name": f"r{n}", "html_url": "u", "published_at": "2026-07-09T00:00:00Z"}
        for n in range(50)
    ]
    api = PollApi(pages={"/releases": [page] * 11})  # more full pages than the cap
    gateway = make_gateway(api)
    with caplog.at_level(logging.INFO, logger="blybot"):
        _events, _cursor = await gateway.poll_resource("x/y", "tok", Resource.RELEASES, WM)
    assert api.seen_pages == list(range(1, 11))  # stopped at the 10-page cap
    assert any("repo_poll" in message for message in caplog.messages)
    await gateway.aclose()


async def test_baseline_without_a_usable_date_header_stays_unstamped() -> None:
    for date in (None, "not a valid date"):
        api = PollApi({"/issues": ISSUES}, date=date)
        gateway = make_gateway(api)
        _events, cursor = await gateway.poll_resource("x/y", "tok", Resource.ISSUES, None)
        assert cursor == ""  # cannot stamp now → retries baseline next cycle
        await gateway.aclose()
