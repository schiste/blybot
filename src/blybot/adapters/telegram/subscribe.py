"""DM digest-subscription commands (SPECIFICATION §21).

Members subscribe to a scope's digest privately: a subscribe deep link
(minted by ``/subscribable`` in the group) arms a pending target here, and
``/subscribe`` creates the durable subscription. ``/mysubs`` lists and
``/unsubscribe`` removes them. The subscriber's DM chat id — the one
durable Telegram identifier — lives only in these handlers and the
subscription store, never in the pseudonymized content layer.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Final

from blybot.adapters.telegram._common import dm_scope
from blybot.domain.ports import StorageError

if TYPE_CHECKING:
    from telegram import Update
    from telegram.ext import ContextTypes

    from blybot.domain.models import PlatformCapabilities, Scope
    from blybot.domain.ports import ProfileStore, SubscriptionStore
    from blybot.services.commands import CommandService
    from blybot.services.subscriptions import SubscriptionBinding

REPLY_STORAGE_DOWN: Final = "That's temporarily unavailable — please try again later."
REPLY_LINK_INVALID: Final = (
    "That subscribe link is no longer valid. Ask an admin to run /subscribable on "
    "in the chat and share a fresh link."
)
REPLY_SUBSCRIBE_PROMPT: Final = (
    "You're subscribing to this chat's digest. Send /subscribe for the daily default, or "
    "/subscribe <schedule> <recipe> lang:xx — e.g. /subscribe daily@08:00 summarize lang:fr.\n"
    "Schedules: daily@HH:MM, every:6h, weekly@mon.09:00. Recipes: summarize, talking_points, stats."
)
REPLY_NO_PENDING: Final = (
    "Tap a subscribe link an admin shared for the chat you want, then run /subscribe."
)


@dataclass(eq=False)
class SubscriptionHandlers:
    """DM commands for opt-in digest subscriptions."""

    profiles: ProfileStore
    subscriptions: SubscriptionStore
    commands: CommandService
    binding: SubscriptionBinding
    default_lang: str
    # Subscriptions deliver via durable DMs; admission is gated on the
    # platform advertising that capability.
    capabilities: PlatformCapabilities

    async def redeem_link(self, context: ContextTypes.DEFAULT_TYPE, dm: Scope, code: str) -> None:
        """Resolve a tapped ``sub_<code>`` link and arm the /subscribe prompt."""
        try:
            profile = await self.profiles.get_by_subscribe_code(code)
        except StorageError:
            await _reply(context, dm, REPLY_STORAGE_DOWN)
            return
        if profile is None:
            await _reply(context, dm, REPLY_LINK_INVALID)
            return
        self.binding.open_entry(dm, profile.scope)
        await _reply(context, dm, REPLY_SUBSCRIBE_PROMPT)

    async def on_subscribe(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Create a subscription for the scope the tapped link armed."""
        chat = update.effective_chat
        if chat is None:
            return
        dm = dm_scope(chat.id)
        # Telegram's own step: a shared link armed which scope this DM is
        # about. Discord takes the channel the command ran in instead.
        source = self.binding.pending_target(dm)
        if source is None:
            await _reply(context, dm, REPLY_NO_PENDING)
            return
        result = await self.commands.subscribe(source, dm, options=" ".join(context.args or []))
        if result.ok:
            self.binding.close_entry(dm)
        await _reply(context, dm, result.text)

    async def on_unsubscribe(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Remove one of the caller's subscriptions by id."""
        chat = update.effective_chat
        if chat is None:
            return
        dm = dm_scope(chat.id)
        result = await self.commands.unsubscribe(dm, subscription_id=(context.args or [""])[0])
        await _reply(context, dm, result.text)

    async def on_mysubs(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """List the caller's subscriptions."""
        chat = update.effective_chat
        if chat is None:
            return
        dm = dm_scope(chat.id)
        result = await self.commands.list_subscriptions(dm)
        await _reply(context, dm, result.text)


async def _reply(context: ContextTypes.DEFAULT_TYPE, dm: Scope, text: str) -> None:
    await context.bot.send_message(chat_id=int(dm.channel), text=text)
