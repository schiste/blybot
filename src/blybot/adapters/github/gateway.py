"""GitHub gateway for group-bound repositories (spec v2, Phase B).

Unlike :mod:`blybot.adapters.github.issues` (the bot's own tracker, one
operator token), this gateway serves many groups: every call carries
the *group's* token, fetched decrypted from the vault just-in-time and
never cached here.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC
from email.utils import parsedate_to_datetime
from typing import TYPE_CHECKING, Any, Final

import httpx

from blybot.domain.models import EventType, RepoEvent, RepoSummary, Resource
from blybot.domain.ports import IssueTrackerError
from blybot.observability import log_event

if TYPE_CHECKING:
    from collections.abc import Callable

_API: Final = "https://api.github.com"
_OK: Final = 200
_CREATED: Final = 201
_RECENT: Final = 3
_PER_PAGE: Final = 50
_PAGE_CAP: Final = 10  # at most 500 items/resource/poll for `since`-less streams
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

    async def poll_resource(
        self, repo: str, token: str, resource: Resource, cursor: str | None
    ) -> tuple[list[RepoEvent], str]:
        """Return enriched events from one resource stream, plus a new cursor.

        The cursor is an ISO-8601 ``updated_at`` watermark. A falsy
        cursor baselines: it stamps the watermark at the server's current
        time and emits nothing, so enabling a rule never replays history
        yet the first event *after* enabling still fires (even on a
        stream that was empty at enable time). Each item yields zero or
        more events (a PR both opened and merged in one window fires
        both); an event fires only when its own transition timestamp is
        newer than the watermark.

        Streams without a ``since`` parameter (pulls, releases) are
        paginated up to :data:`_PAGE_CAP`; a saturated fetch is logged as
        a possible gap rather than silently dropping events.
        """
        spec = _SPECS[resource]
        watermark = cursor or ""
        items, server_now, saturated = await self._fetch_pages(repo, token, spec, watermark)
        if not watermark:  # baseline: stamp "now", never replay history
            return [], server_now
        try:
            fired, new_watermark = _collect(spec, items, watermark)
        except (KeyError, ValueError, TypeError, AttributeError) as error:
            # One malformed item must degrade this poll, not kill it.
            msg = f"malformed {resource.value} payload"
            raise IssueTrackerError(msg) from error
        if saturated:
            log_event("repo_poll", "ignored", pages=_PAGE_CAP)
        fired.sort(key=lambda pair: pair[0])  # oldest transition first
        return [event for _, event in fired], new_watermark

    async def _fetch_pages(
        self, repo: str, token: str, spec: _ResourceSpec, watermark: str
    ) -> tuple[list[dict[str, Any]], str, bool]:
        """Fetch pages until drained or capped; return (items, server_now, saturated).

        Stops early on a short page, or — for a stream ordered by its own
        watermark (pulls) — once a page ends at or before the watermark.
        ``saturated`` means the page cap was hit with full pages, i.e. a
        possible gap.
        """
        items: list[dict[str, Any]] = []
        server_now = watermark
        saturated = False
        for page in range(1, _PAGE_CAP + 1):
            try:
                response = await self._client.get(
                    f"{_API}/repos/{repo}/{spec.path}",
                    headers=_auth(token),
                    params={**spec.params(watermark), "page": page},
                )
            except httpx.HTTPError as error:
                msg = "could not reach GitHub"
                raise IssueTrackerError(msg) from error
            if response.status_code != _OK:
                msg = f"{spec.path} poll failed: HTTP {response.status_code}"
                raise IssueTrackerError(msg)
            if page == 1:
                server_now = _server_time(response) or watermark
            try:
                batch = response.json()
                items.extend(batch)
                short = len(batch) < _PER_PAGE
                past = bool(
                    spec.self_ordered
                    and watermark
                    and batch
                    and spec.watermark(batch[-1]) <= watermark
                )
            except (KeyError, ValueError, TypeError, AttributeError) as error:
                msg = f"malformed {spec.path} payload"
                raise IssueTrackerError(msg) from error
            if short or past:
                break
        else:
            saturated = True
        return items, server_now, saturated

    async def aclose(self) -> None:
        """Release the underlying HTTP client."""
        await self._client.aclose()


def _auth(token: str) -> dict[str, Any]:
    return {"Authorization": f"Bearer {token}"}


def _clip(title: str) -> str:
    return title[:_TITLE_CAP] + ("…" if len(title) > _TITLE_CAP else "")


def _server_time(response: httpx.Response) -> str:
    """The response ``Date`` header as an ISO-8601 UTC ``Z`` string, or ``""``.

    Used as the baseline watermark so the first event after enabling
    fires even when the stream was empty at enable time.
    """
    raw = response.headers.get("Date")
    if not raw:
        return ""
    try:
        moment = parsedate_to_datetime(raw)
    except (TypeError, ValueError):
        return ""
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass(frozen=True, slots=True)
class _ResourceSpec:
    """How to fetch and normalize one GitHub resource stream."""

    path: str
    params: Callable[[str], dict[str, Any]]
    # Timestamp used to advance the watermark (usually ``updated_at``).
    watermark: Callable[[dict[str, Any]], str]
    # (transition timestamp, event) pairs an item yields.
    events: Callable[[dict[str, Any]], list[tuple[str, RepoEvent]]]
    # Whether the stream is server-ordered by its own watermark field
    # (desc), so pagination can stop once a page ends at/under it.
    self_ordered: bool = False


def _collect(
    spec: _ResourceSpec, items: list[dict[str, Any]], watermark: str
) -> tuple[list[tuple[str, RepoEvent]], str]:
    """Advance the watermark over ``items`` and gather events past it.

    Called only for a non-baseline poll (a truthy ``watermark``); the
    baseline is handled by the caller so no history is replayed.
    """
    new_watermark = watermark
    fired: list[tuple[str, RepoEvent]] = []
    for item in items:
        new_watermark = max(new_watermark, spec.watermark(item))
        fired.extend((ts, event) for ts, event in spec.events(item) if ts > watermark)
    return fired, new_watermark


def _updated_since(watermark: str, **extra: str) -> dict[str, Any]:
    params: dict[str, Any] = {"sort": "updated", "direction": "asc", "per_page": _PER_PAGE, **extra}
    if watermark:
        params["since"] = watermark
    return params


def _recent_page(_watermark: str) -> dict[str, Any]:
    # For endpoints without a `since` param: take the most-recently-updated page.
    return {"state": "all", "sort": "updated", "direction": "desc", "per_page": _PER_PAGE}


def _actor(item: dict[str, Any], key: str = "user") -> str:
    return str((item.get(key) or {}).get("login", ""))


def _labels(item: dict[str, Any]) -> frozenset[str]:
    return frozenset(str(label["name"]) for label in item.get("labels", []) if label.get("name"))


def _assignees(item: dict[str, Any]) -> frozenset[str]:
    return frozenset(str(one["login"]) for one in item.get("assignees", []) if one.get("login"))


def _milestone(item: dict[str, Any]) -> str:
    return str((item.get("milestone") or {}).get("title", ""))


def _issue_events(item: dict[str, Any]) -> list[tuple[str, RepoEvent]]:
    def make(event_type: EventType) -> RepoEvent:
        return RepoEvent(
            event_type=event_type,
            title=_clip(str(item.get("title", "?"))),
            url=str(item.get("html_url", "")),
            author=_actor(item),
            labels=_labels(item),
            assignees=_assignees(item),
            milestone=_milestone(item),
            state=str(item.get("state", "")),
        )

    out: list[tuple[str, RepoEvent]] = []
    if created := str(item.get("created_at") or ""):
        out.append((created, make(EventType.ISSUE_OPENED)))
    if (closed := str(item.get("closed_at") or "")) and item.get("state") == "closed":
        out.append((closed, make(EventType.ISSUE_CLOSED)))
    return out


def _pull_events(item: dict[str, Any]) -> list[tuple[str, RepoEvent]]:
    def make(event_type: EventType) -> RepoEvent:
        return RepoEvent(
            event_type=event_type,
            title=_clip(str(item.get("title", "?"))),
            url=str(item.get("html_url", "")),
            author=_actor(item),
            labels=_labels(item),
            assignees=_assignees(item),
            milestone=_milestone(item),
            base_branch=str((item.get("base") or {}).get("ref", "")),
            draft=bool(item.get("draft")),
            state=str(item.get("state", "")),
        )

    out: list[tuple[str, RepoEvent]] = []
    if created := str(item.get("created_at") or ""):
        out.append((created, make(EventType.PR_OPENED)))
    if merged := str(item.get("merged_at") or ""):
        out.append((merged, make(EventType.PR_MERGED)))
    elif (closed := str(item.get("closed_at") or "")) and item.get("state") == "closed":
        out.append((closed, make(EventType.PR_CLOSED)))
    return out


def _comment_events(item: dict[str, Any]) -> list[tuple[str, RepoEvent]]:
    created = str(item.get("created_at") or "")
    if not created:
        return []
    return [
        (
            created,
            RepoEvent(
                event_type=EventType.COMMENT,
                title=_clip(str(item.get("body", "")).strip() or "(comment)"),
                url=str(item.get("html_url", "")),
                author=_actor(item),
            ),
        )
    ]


def _release_events(item: dict[str, Any]) -> list[tuple[str, RepoEvent]]:
    published = str(item.get("published_at") or "")
    if not published:  # draft / unpublished release
        return []
    name = item.get("name") or item.get("tag_name") or "?"
    return [
        (
            published,
            RepoEvent(
                event_type=EventType.RELEASE,
                title=_clip(str(name)),
                url=str(item.get("html_url", "")),
                author=_actor(item, "author"),
            ),
        )
    ]


_SPECS: Final = {
    Resource.ISSUES: _ResourceSpec(
        path="issues",
        params=lambda wm: _updated_since(wm, state="all"),
        watermark=lambda item: str(item.get("updated_at") or ""),
        # The /issues list returns PRs too; the pull_request key marks them.
        events=lambda item: [] if item.get("pull_request") else _issue_events(item),
    ),
    Resource.PULLS: _ResourceSpec(
        path="pulls",  # /pulls has no `since` param; paginate, ordered by updated desc
        params=_recent_page,
        watermark=lambda item: str(item.get("updated_at") or ""),
        events=_pull_events,
        self_ordered=True,
    ),
    Resource.ISSUE_COMMENTS: _ResourceSpec(
        path="issues/comments",
        params=_updated_since,
        watermark=lambda item: str(item.get("updated_at") or ""),
        events=_comment_events,
    ),
    Resource.RELEASES: _ResourceSpec(
        path="releases",
        params=lambda wm: {"per_page": _PER_PAGE},  # noqa: ARG005 -- no `since` on releases
        watermark=lambda item: str(item.get("published_at") or ""),
        events=_release_events,
    ),
}
