"""GitHub gateway for group-bound repositories (spec v2, Phase B).

Unlike :mod:`blybot.adapters.github.issues` (the bot's own tracker, one
operator token), this gateway serves many groups: every call carries
the *group's* token, fetched decrypted from the vault just-in-time and
never cached here.
"""

from __future__ import annotations

from typing import Any, Final

import httpx

from blybot.domain.models import EventType, RepoEvent, RepoSummary
from blybot.domain.ports import IssueTrackerError
from blybot.observability import log_event

_API: Final = "https://api.github.com"
_OK: Final = 200
_CREATED: Final = 201
_NOT_MODIFIED: Final = 304
_RECENT: Final = 3
_EVENTS_PAGE: Final = 30
_TITLE_CAP: Final = 120


class GitHubRepoGateway:
    """:class:`blybot.domain.ports.RepoGateway` over the GitHub REST API."""

    def __init__(
        self,
        user_agent: str,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._client = httpx.AsyncClient(
            headers={"Accept": "application/vnd.github+json", "User-Agent": user_agent},
            timeout=httpx.Timeout(30.0),
            transport=transport,
        )

    async def validate_token(self, repo: str, token: str) -> bool:
        """Whether the token can see the repo and write its issues."""
        try:
            response = await self._client.get(f"{_API}/repos/{repo}", headers=_auth(token))
        except httpx.HTTPError:
            return False
        if response.status_code != _OK:
            return False
        permissions = response.json().get("permissions", {})
        # Fine-grained "Issues: write" surfaces as push=False but
        # triage/push grants; accept any writing-ish permission.
        return bool(permissions.get("push") or permissions.get("triage"))

    async def open_issue(self, repo: str, token: str, title: str, body: str) -> str:
        """Create an issue in the bound repo; return its public URL."""
        try:
            response = await self._client.post(
                f"{_API}/repos/{repo}/issues",
                headers=_auth(token),
                json={"title": title, "body": body},
            )
        except httpx.HTTPError as error:
            msg = "could not reach GitHub"
            raise IssueTrackerError(msg) from error
        if response.status_code != _CREATED:
            msg = f"issue creation failed: HTTP {response.status_code}"
            raise IssueTrackerError(msg)
        return str(response.json()["html_url"])

    async def open_summary(self, repo: str, token: str) -> RepoSummary:
        """Return a small open-items summary of the bound repo."""
        try:
            repo_response = await self._client.get(f"{_API}/repos/{repo}", headers=_auth(token))
            issues_response = await self._client.get(
                f"{_API}/repos/{repo}/issues",
                headers=_auth(token),
                params={"state": "open", "per_page": _RECENT},
            )
        except httpx.HTTPError as error:
            msg = "could not reach GitHub"
            raise IssueTrackerError(msg) from error
        if repo_response.status_code != _OK or issues_response.status_code != _OK:
            msg = "repository summary unavailable"
            raise IssueTrackerError(msg)
        return RepoSummary(
            repo=repo,
            open_count=int(repo_response.json().get("open_issues_count", 0)),
            recent_titles=tuple(str(item["title"]) for item in issues_response.json()[:_RECENT]),
        )

    async def events_since(
        self, repo: str, token: str, cursor: str | None
    ) -> tuple[list[RepoEvent], str]:
        """Return events newer than ``cursor`` plus the advanced cursor.

        The cursor is ``"<etag>|<last event id>"``: the ETag makes
        steady-state polls free (304), the id watermark prevents
        re-announcing events already delivered. A ``None`` cursor
        baselines at the current head without replaying history.
        """
        etag, last_id = _split_cursor(cursor)
        headers = _auth(token)
        if etag:
            headers["If-None-Match"] = etag
        try:
            response = await self._client.get(
                f"{_API}/repos/{repo}/events",
                headers=headers,
                params={"per_page": _EVENTS_PAGE},
            )
        except httpx.HTTPError as error:
            msg = "could not reach GitHub"
            raise IssueTrackerError(msg) from error
        if response.status_code == _NOT_MODIFIED:
            return [], cursor or ""
        if response.status_code != _OK:
            msg = f"event poll failed: HTTP {response.status_code}"
            raise IssueTrackerError(msg)
        try:
            raw_events = response.json()
            new_etag = response.headers.get("ETag", "")
            newest_id = max((int(item["id"]) for item in raw_events), default=last_id)
            if cursor is None:  # baseline: never replay history
                return [], _join_cursor(new_etag, newest_id)
            fresh = [item for item in raw_events if int(item["id"]) > last_id]
            events = [event for item in reversed(fresh) if (event := _reduce(item)) is not None]
        except (KeyError, ValueError, TypeError, AttributeError) as error:
            # One malformed payload must degrade this poll, not kill it.
            msg = "malformed events payload"
            raise IssueTrackerError(msg) from error
        if len(fresh) == len(raw_events) >= _EVENTS_PAGE:
            # Every fetched event was new: more may exist beyond page 1.
            log_event("repo_events", "ignored")
        return events, _join_cursor(new_etag, newest_id)

    async def aclose(self) -> None:
        """Release the underlying HTTP client."""
        await self._client.aclose()


def _auth(token: str) -> dict[str, Any]:
    return {"Authorization": f"Bearer {token}"}


def _split_cursor(cursor: str | None) -> tuple[str, int]:
    if not cursor or "|" not in cursor:
        return "", 0
    etag, _, last_id = cursor.rpartition("|")
    return etag, int(last_id or 0)


def _join_cursor(etag: str, last_id: int) -> str:
    return f"{etag}|{last_id}"


def _clip(title: str) -> str:
    return title[:_TITLE_CAP] + ("…" if len(title) > _TITLE_CAP else "")


def _reduce(item: dict[str, Any]) -> RepoEvent | None:
    """Map a raw GitHub event to a publishable RepoEvent, or None."""
    kind = item.get("type")
    payload = item.get("payload", {})
    if kind == "ReleaseEvent" and payload.get("action") == "published":
        release = payload.get("release", {})
        return RepoEvent(
            event_type=EventType.RELEASE,
            title=_clip(f"Release {release.get('name') or release.get('tag_name', '?')}"),
            url=str(release.get("html_url", "")),
        )
    if kind == "PullRequestEvent" and payload.get("action") == "closed":
        pull = payload.get("pull_request", {})
        if not pull.get("merged"):
            return None
        return RepoEvent(
            event_type=EventType.PR_MERGED,
            title=_clip(f"Merged: {pull.get('title', '?')}"),
            url=str(pull.get("html_url", "")),
        )
    if kind == "IssuesEvent" and payload.get("action") == "opened":
        issue = payload.get("issue", {})
        return RepoEvent(
            event_type=EventType.ISSUE_OPENED,
            title=_clip(f"New issue: {issue.get('title', '?')}"),
            url=str(issue.get("html_url", "")),
        )
    return None
