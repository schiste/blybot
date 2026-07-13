"""The GitHub-token entry flow (spec v2): configuration deep link → paste.

Separated from :class:`~blybot.adapters.telegram.handlers.PrivateHandlers`
(pseudonymous DM transcription) — the two share only the private-chat
entry points. A ``cfg_<nonce>`` deep link arms an entry for the nonce's
group; the admin's *next* DM is claimed here (never transcribed), the
pasted secret deleted, validated against the bound repo, and stored
encrypted.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Final

from telegram.error import TelegramError

from blybot.adapters.telegram.admin import is_group_admin
from blybot.domain.ports import StorageError
from blybot.observability import log_event

if TYPE_CHECKING:
    from telegram import Update
    from telegram.ext import ContextTypes

    from blybot.domain.ports import RepoActions, TokenVault
    from blybot.observability import Counters
    from blybot.services.binding import TokenBinding
    from blybot.services.directory import ChannelDirectory

REPLY_LINK_EXPIRED: Final = (
    "That configuration link is no longer valid. Run /setrepo in the group again for a fresh one."
)
REPLY_LINK_NOT_ADMIN: Final = "Only an admin of that group can supply its token."
REPLY_PAT_PROMPT: Final = (
    "Paste the GitHub token for {repo} as your next message here. Use a "
    "fine-grained PAT restricted to that repository with Issues read/write "
    "only. I'll validate it, encrypt it, store it — and delete your message "
    "from this chat immediately. This prompt expires in 5 minutes; while "
    "it's active, nothing you send me is transcribed."
)
REPLY_PAT_NO_REPO: Final = "That group no longer has a repository bound; run /setrepo there first."
REPLY_PAT_INVALID: Final = (
    "GitHub rejected that token for the bound repository — check the repo "
    "access and Issues permission, then paste it again."
)
REPLY_PAT_STORE_FAILED: Final = (
    "Storing the token failed on my side — please paste it again in a moment."
)
REPLY_PAT_SAVED: Final = (
    "Token validated, encrypted and stored; I've deleted your message. "
    "/issue and /repo are live in the group; /revoke there discards the token."
)


@dataclass
class TokenEntryHandler:
    """Redeems configuration links and captures the pasted GitHub token."""

    binding: TokenBinding
    directory: ChannelDirectory
    gateway: RepoActions | None
    vault: TokenVault | None
    counters: Counters

    def claims_next_message(self, dm_chat_id: int) -> tuple[int, int] | None:
        """The (group, topic) awaiting a token in this DM, if entry is armed."""
        return self.binding.pending_target(dm_chat_id)

    async def redeem_link(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE, dm_chat_id: int, nonce: str
    ) -> None:
        """Validate a ``cfg_<nonce>`` deep link and arm the token prompt."""
        target = self.binding.peek_link(nonce)
        message = update.effective_message
        user = message.from_user if message else None
        if target is None:
            await self._reply(context, dm_chat_id, REPLY_LINK_EXPIRED)
            return
        group_chat_id, thread_id = target
        if user is None or not await is_group_admin(context.bot, group_chat_id, user.id):
            # Deliberately NOT consumed: a non-admin tapping the public
            # link must not burn it for the real admin.
            await self._reply(context, dm_chat_id, REPLY_LINK_NOT_ADMIN)
            return
        if self.binding.redeem_link(nonce) is None:  # consumed in a race
            await self._reply(context, dm_chat_id, REPLY_LINK_EXPIRED)
            return
        settings = await self.directory.resolve(group_chat_id, thread_id)
        if not settings.repo:
            await self._reply(context, dm_chat_id, REPLY_PAT_NO_REPO)
            return
        self.binding.open_entry(dm_chat_id, group_chat_id, thread_id)
        log_event("token_entry_opened", "ok")
        await self._reply(context, dm_chat_id, REPLY_PAT_PROMPT.format(repo=settings.repo))

    async def accept_token(
        self,
        context: ContextTypes.DEFAULT_TYPE,
        dm_chat_id: int,
        target: tuple[int, int],
        message_id: int,
        text: str,
    ) -> None:
        """Delete the pasted secret, then validate and store the token."""
        # Remove the pasted secret from the chat first — bots may delete
        # messages in private chats, so don't rely on the admin doing it.
        try:
            await context.bot.delete_message(chat_id=dm_chat_id, message_id=message_id)
        except TelegramError:
            log_event("command_cleanup", "ignored")
        group_chat_id, thread_id = target
        if self.gateway is None or self.vault is None:
            self.binding.close_entry(dm_chat_id)
            await self._reply(context, dm_chat_id, REPLY_PAT_NO_REPO)
            return
        settings = await self.directory.resolve(group_chat_id, thread_id)
        if not settings.repo:
            self.binding.close_entry(dm_chat_id)
            await self._reply(context, dm_chat_id, REPLY_PAT_NO_REPO)
            return
        token = text.strip()
        if not await self.gateway.validate_token(settings.repo, token):
            await self._reply(context, dm_chat_id, REPLY_PAT_INVALID)  # stays armed for a retry
            return
        try:
            await self.vault.store_token(group_chat_id, thread_id, token)
        except StorageError:
            await self._reply(context, dm_chat_id, REPLY_PAT_STORE_FAILED)  # stays armed
            return
        self.binding.close_entry(dm_chat_id)
        self.counters.increment("tokens_bound")
        log_event("token_bound", "ok")
        await self._reply(context, dm_chat_id, REPLY_PAT_SAVED)

    @staticmethod
    async def _reply(context: ContextTypes.DEFAULT_TYPE, dm_chat_id: int, text: str) -> None:
        await context.bot.send_message(chat_id=dm_chat_id, text=text)
