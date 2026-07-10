"""GitHub gateway for group-bound repositories (spec v2, Phase B).

Unlike :mod:`blybot.adapters.github.issues` (the bot's own tracker, one
operator token), this gateway serves many groups: every call carries
the *group's* token, fetched decrypted from the vault just-in-time and
never cached here.
"""

from __future__ import annotations

from typing import Any, Final

import httpx

from blybot.domain.models import RepoSummary
from blybot.domain.ports import IssueTrackerError

_API: Final = "https://api.github.com"
_OK: Final = 200
_CREATED: Final = 201
_RECENT: Final = 3


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

    async def aclose(self) -> None:
        """Release the underlying HTTP client."""
        await self._client.aclose()


def _auth(token: str) -> dict[str, Any]:
    return {"Authorization": f"Bearer {token}"}
