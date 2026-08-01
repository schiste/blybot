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
from blybot.observability import log_event

if TYPE_CHECKING:
    from telegram import Update
    from telegram.ext import ContextTypes

    from blybot.domain.models import Scope
    from blybot.services.binding import TokenBinding
    from blybot.services.commands import CommandService
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
# Validation/storage wording now lives in the neutral CommandService (Discord's
# modal renders the same strings); only the deletion note — Telegram's own
# affordance, since it can remove the pasted secret — is appended here.
REPLY_PAT_DELETED: Final = "I've also deleted your message."


@dataclass
class TokenEntryHandler:
    """Redeems configuration links and captures the pasted GitHub token."""

    binding: TokenBinding
    directory: ChannelDirectory
    commands: CommandService

    def claims_next_message(self, dm: Scope) -> Scope | None:
        """The group scope awaiting a token in this DM, if entry is armed."""
        return self.binding.pending_target(dm)

    async def redeem_link(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE, dm: Scope, nonce: str
    ) -> None:
        """Validate a ``cfg_<nonce>`` deep link and arm the token prompt."""
        target = self.binding.peek_link(nonce)
        message = update.effective_message
        user = message.from_user if message else None
        if target is None:
            await self._reply(context, dm, REPLY_LINK_EXPIRED)
            return
        if user is None or not await is_group_admin(context.bot, int(target.channel), user.id):
            # Deliberately NOT consumed: a non-admin tapping the public
            # link must not burn it for the real admin.
            await self._reply(context, dm, REPLY_LINK_NOT_ADMIN)
            return
        if self.binding.redeem_link(nonce) is None:  # consumed in a race
            await self._reply(context, dm, REPLY_LINK_EXPIRED)
            return
        settings = await self.directory.resolve(target)
        if not settings.repo:
            await self._reply(context, dm, REPLY_PAT_NO_REPO)
            return
        self.binding.open_entry(dm, target)
        log_event("token_entry_opened", "ok")
        await self._reply(context, dm, REPLY_PAT_PROMPT.format(repo=settings.repo))

    async def accept_token(  # noqa: PLR0913, PLR0917 -- the flattened message facts
        self,
        context: ContextTypes.DEFAULT_TYPE,
        dm: Scope,
        target: Scope,
        message_id: int,
        text: str,
        user_id: int | None,
    ) -> None:
        """Delete the pasted secret, then validate and store the token."""
        # Remove the pasted secret from the chat first — bots may delete
        # messages in private chats, so don't rely on the admin doing it.
        # This runs before authorization on purpose: the secret is already
        # in the chat either way, so it is scrubbed even from a caller who
        # turns out to be no longer allowed to supply it.
        try:
            await context.bot.delete_message(chat_id=int(dm.channel), message_id=message_id)
        except TelegramError:
            log_event("command_cleanup", "ignored")
        # Re-verify admin-ship HERE, not only when the link was redeemed
        # (issue #27). Redemption can be up to ``entry_ttl`` earlier, and a
        # caller demoted in between must not be able to finish binding a
        # token to the group. Discord's TokenModal re-checks on submit for
        # the same reason.
        # A message with no identifiable sender cannot prove anything, so it
        # is denied outright rather than looked up under a placeholder id.
        if user_id is None or not await is_group_admin(context.bot, int(target.channel), user_id):
            self.binding.close_entry(dm)
            log_event("token_entry_denied", "ignored")
            await self._reply(context, dm, REPLY_LINK_NOT_ADMIN)
            return
        # A vanished repo binding closes the flow outright; every other
        # failure leaves the prompt armed so the admin can paste again.
        settings = await self.directory.resolve(target)
        if not settings.repo:
            self.binding.close_entry(dm)
            await self._reply(context, dm, REPLY_PAT_NO_REPO)
            return
        # Validating the secret against the bound repo and encrypting it into
        # the vault is identical on every platform, so it lives in the neutral
        # CommandService; admin-ship was just re-proven above.
        result = await self.commands.store_token(target, is_admin=True, token=text)
        if not result.ok:
            await self._reply(context, dm, result.text)
            return
        self.binding.close_entry(dm)
        await self._reply(context, dm, f"{result.text} {REPLY_PAT_DELETED}")

    @staticmethod
    async def _reply(context: ContextTypes.DEFAULT_TYPE, dm: Scope, text: str) -> None:
        await context.bot.send_message(chat_id=int(dm.channel), text=text)
