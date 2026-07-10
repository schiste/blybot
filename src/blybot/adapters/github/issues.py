"""GitHub issue tracker adapter.

One authenticated POST per report. The token should be a fine-grained
PAT restricted to the repository with Issues read/write only.
"""

from __future__ import annotations

import httpx

from blybot.domain.ports import IssueTrackerError

_CREATED = 201


class GitHubIssueTracker:
    """:class:`blybot.domain.ports.IssueTracker` backed by the GitHub REST API."""

    def __init__(
        self,
        repo: str,
        token: str,
        user_agent: str,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._url = f"https://api.github.com/repos/{repo}/issues"
        self._client = httpx.AsyncClient(
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
                "User-Agent": user_agent,
            },
            timeout=httpx.Timeout(30.0),
            transport=transport,
        )

    async def open_issue(self, title: str, body: str) -> str:
        """Create an issue; return its public URL."""
        try:
            response = await self._client.post(self._url, json={"title": title, "body": body})
        except httpx.HTTPError as error:
            msg = "could not reach the issue tracker"
            raise IssueTrackerError(msg) from error
        if response.status_code != _CREATED:
            msg = f"issue creation failed: HTTP {response.status_code}"
            raise IssueTrackerError(msg)
        return str(response.json()["html_url"])

    async def aclose(self) -> None:
        """Release the underlying HTTP client."""
        await self._client.aclose()
