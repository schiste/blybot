"""Group-bound repository actions: /issue filing and /repo summaries.

Issues are filed with the group's own encrypted token and composed with
the same hardening as /bug: verbatim code-block body, no pings, no
reporter identity anywhere (R6).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Final

from blybot.services.feedback import as_code_block, issue_title

if TYPE_CHECKING:
    from blybot.domain.models import RepoSummary
    from blybot.domain.ports import RepoGateway, TokenVault
    from blybot.services.directory import ChannelDirectory

_BODY_PREAMBLE: Final = (
    "Filed anonymously from the group's Telegram chat (`/issue`). "
    "No reporter identity is recorded.\n\n"
)


class NoRepoBoundError(Exception):
    """The group has not bound a repository."""


class NoTokenError(Exception):
    """The group bound a repository but never completed the token step."""


@dataclass(eq=False)
class GroupRepoService:
    """Files issues and reads summaries with the group's own token."""

    gateway: RepoGateway
    vault: TokenVault
    directory: ChannelDirectory

    async def file_issue(self, chat_id: int, thread_id: int, text: str) -> str:
        """File ``text`` as an anonymous issue in the topic's repo; return its URL."""
        repo, token = await self._binding(chat_id, thread_id)
        return await self.gateway.open_issue(
            repo, token, title=issue_title(text), body=_BODY_PREAMBLE + as_code_block(text)
        )

    async def summary(self, chat_id: int, thread_id: int) -> RepoSummary:
        """Return the topic's repo open-items summary."""
        repo, token = await self._binding(chat_id, thread_id)
        return await self.gateway.open_summary(repo, token)

    async def _binding(self, chat_id: int, thread_id: int) -> tuple[str, str]:
        settings = await self.directory.resolve(chat_id, thread_id)
        if not settings.repo:
            raise NoRepoBoundError
        # The token lives with whichever tier bound the repo (a topic
        # inheriting the group repo uses the group's token).
        token = await self.vault.fetch_token(chat_id, settings.repo_thread_id)
        if not token:
            raise NoTokenError
        return settings.repo, token
