"""Group-bound repository actions: /issue filing and /repo summaries.

Issues are filed with the group's own encrypted token and composed with
the same hardening as /bug: verbatim code-block body, no pings, no
reporter identity anywhere (R6).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Final

from blybot.services.feedback import compose_issue

if TYPE_CHECKING:
    from blybot.domain.models import RepoSummary, Scope
    from blybot.domain.ports import RepoActions, TokenVault
    from blybot.services.directory import ChannelDirectory

_BODY_PREAMBLE: Final = (
    "Filed anonymously from the chat bound to this repository (`/issue`). "
    "No reporter identity is recorded.\n\n"
)


class NoRepoBoundError(Exception):
    """The group has not bound a repository."""


class NoTokenError(Exception):
    """The group bound a repository but never completed the token step."""


@dataclass(eq=False)
class GroupRepoService:
    """Files issues and reads summaries with the group's own token."""

    gateway: RepoActions
    vault: TokenVault
    directory: ChannelDirectory

    async def file_issue(self, scope: Scope, text: str) -> str:
        """File ``text`` as an anonymous issue in the scope's repo; return its URL."""
        repo, token = await self._binding(scope)
        title, body = compose_issue(text, _BODY_PREAMBLE)
        return await self.gateway.open_issue(repo, token, title=title, body=body)

    async def summary(self, scope: Scope) -> RepoSummary:
        """Return the scope's repo open-items summary."""
        repo, token = await self._binding(scope)
        return await self.gateway.open_summary(repo, token)

    async def _binding(self, scope: Scope) -> tuple[str, str]:
        settings = await self.directory.resolve(scope)
        if not settings.repo:
            raise NoRepoBoundError
        # The token lives with whichever tier bound the repo (a topic
        # inheriting the group repo uses the group's token).
        token = await self.vault.fetch_token(settings.repo_scope)
        if not token:
            raise NoTokenError
        return settings.repo, token
