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
from blybot.domain.subscriptions import Subscription
from blybot.observability import log_event
from blybot.services.subscriptions import SubscriptionParseError, mint_sub_id, parse_subscription

if TYPE_CHECKING:
    from telegram import Update
    from telegram.ext import ContextTypes

    from blybot.domain.models import Scope
    from blybot.domain.ports import ProfileStore, SubscriptionStore
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
REPLY_SUBSCRIBED: Final = (
    "Subscribed [{sub_id}]: {schedule} {recipe} ({lang}) — it arrives here. "
    "/mysubs to review, /unsubscribe {sub_id} to stop."
)
REPLY_UNSUB_USAGE: Final = "Usage: /unsubscribe <id> — see your ids with /mysubs"
REPLY_UNSUBSCRIBED: Final = "Unsubscribed."
REPLY_NO_SUCH_SUB: Final = "No subscription with that id is yours."
REPLY_NO_SUBS: Final = "You have no digest subscriptions. Tap a subscribe link to start one."
REPLY_SUBS_HEADER: Final = "Your digest subscriptions:"


@dataclass(eq=False)
class SubscriptionHandlers:
    """DM commands for opt-in digest subscriptions."""

    profiles: ProfileStore
    subscriptions: SubscriptionStore
    binding: SubscriptionBinding
    default_lang: str

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
        source = self.binding.pending_target(dm)
        if source is None:
            await _reply(context, dm, REPLY_NO_PENDING)
            return
        try:
            schedule, recipe, lang = parse_subscription(
                " ".join(context.args or []), self.default_lang
            )
        except SubscriptionParseError as error:
            await _reply(context, dm, str(error))
            return
        subscription = Subscription(
            sub_id=mint_sub_id(),
            dm=dm,
            scope=source,
            schedule=schedule,
            recipe=recipe,
            lang=lang,
        )
        try:
            await self.subscriptions.add(subscription)
        except StorageError:
            await _reply(context, dm, REPLY_STORAGE_DOWN)
            return
        self.binding.close_entry(dm)
        log_event("subscription_add", "ok")
        await _reply(
            context,
            dm,
            REPLY_SUBSCRIBED.format(
                sub_id=subscription.sub_id, schedule=schedule.token, recipe=recipe, lang=lang
            ),
        )

    async def on_unsubscribe(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Remove one of the caller's subscriptions by id."""
        chat = update.effective_chat
        if chat is None:
            return
        dm = dm_scope(chat.id)
        sub_id = ((context.args or [""])[0]).strip()
        if not sub_id:
            await _reply(context, dm, REPLY_UNSUB_USAGE)
            return
        try:
            removed = await self.subscriptions.remove(dm, sub_id)
        except StorageError:
            await _reply(context, dm, REPLY_STORAGE_DOWN)
            return
        await _reply(context, dm, REPLY_UNSUBSCRIBED if removed else REPLY_NO_SUCH_SUB)

    async def on_mysubs(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """List the caller's subscriptions."""
        chat = update.effective_chat
        if chat is None:
            return
        dm = dm_scope(chat.id)
        try:
            subs = await self.subscriptions.list_for_user(dm)
        except StorageError:
            await _reply(context, dm, REPLY_STORAGE_DOWN)
            return
        if not subs:
            await _reply(context, dm, REPLY_NO_SUBS)
            return
        lines = [REPLY_SUBS_HEADER]
        lines += [f"[{s.sub_id}] {s.schedule.token} {s.recipe} ({s.lang})" for s in subs]
        await _reply(context, dm, "\n".join(lines))


async def _reply(context: ContextTypes.DEFAULT_TYPE, dm: Scope, text: str) -> None:
    await context.bot.send_message(chat_id=int(dm.channel), text=text)
