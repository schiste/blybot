"""The Discord gateway: capture ingestion + slash commands.

Two layers, kept apart so the logic stays coverable without a live
connection:

* :class:`DiscordGateway` is pure — plain-argument methods (channel id,
  author id, text, …) that call the neutral services and return either
  ``None`` (ingestion) or a ready-to-send reply string (slash commands).
  Every branch is unit-tested directly, no ``discord`` objects involved.
* :class:`DiscordGatewayClient` is the thin ``discord.Client`` shell: its
  ``on_message`` event and its ``app_commands`` callbacks do nothing but
  pull the plain values off the SDK objects, hand them to the gateway, and
  render the reply. ``run`` is a one-line shell around ``client.run``.

Onboarding is slash commands, not deep links (``deep_links=False``):
``/capture`` and ``/setpage`` are server-admin config, ``/subscribe``,
``/mysubs`` and ``/unsubscribe`` are the durable-DM digest flow. Outbound
digests and reminders go out through the shared neutral delivery loop
driving :class:`~blybot.adapters.discord.transport.DiscordTransport`.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Final

import discord
from discord import app_commands

from blybot.adapters.discord.scope import dm_scope, scope_of
from blybot.domain.models import CapturedMessage
from blybot.domain.ports import StorageError
from blybot.domain.subscriptions import Subscription
from blybot.observability import log_event
from blybot.services.directory import (
    PageNotAllowedError,
    SelfServiceUnavailableError,
)
from blybot.services.subscriptions import (
    SubscriptionParseError,
    mint_sub_id,
    mint_subscribe_code,
    parse_subscription,
)

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable
    from datetime import datetime

    from blybot.domain.models import Scope
    from blybot.domain.ports import SubscriptionStore
    from blybot.services.capture import CaptureService
    from blybot.services.directory import ChannelDirectory
    from blybot.services.policy import GroupPolicy

    from .author_mask import DiscordAuthorMasker

REPLY_NOT_ADMIN: Final = "Only this server's admins can run that command."
REPLY_NOT_ALLOWED: Final = "I'm not configured to serve this channel."
REPLY_STORAGE_DOWN: Final = "That's temporarily unavailable — please try again later."
REPLY_CAPTURE_OFF_DEPLOY: Final = (
    "Message capture isn't enabled on this deployment; ask the operator."
)
REPLY_CAPTURE_ENABLED: Final = (
    "📢 Message capture is now ON here: messages will be archived for on-wiki summaries "
    "and statistics, with authors recorded only as anonymous labels. An admin can stop "
    "this with /capture off."
)
REPLY_CAPTURE_DISABLED: Final = "Message capture is OFF here. The existing archive is kept."
REPLY_SELF_SERVICE_OFF: Final = (
    "Self-service configuration isn't enabled on this deployment; ask the operator."
)
REPLY_PAGE_REFUSED: Final = (
    "That page path isn't valid — give a plain project or user page, e.g. /setpage WikiProject Foo."
)
REPLY_SETPAGE_USAGE: Final = "Usage: /setpage <page path>"
REPLY_PAGE_SET: Final = "Done. Analyses for this channel now publish to {page}."
REPLY_SUBS_UNAVAILABLE: Final = "Digest subscriptions aren't available on this deployment."
REPLY_SUBSCRIBED: Final = (
    "Subscribed [{sub_id}]: {schedule} {recipe} ({lang}) — digests arrive in your DMs. "
    "/mysubs to review, /unsubscribe {sub_id} to stop."
)
REPLY_NO_SUBS: Final = "You have no digest subscriptions. Use /subscribe in a channel to start one."
REPLY_SUBS_HEADER: Final = "Your digest subscriptions:"
REPLY_UNSUB_USAGE: Final = "Usage: /unsubscribe <id> — see your ids with /mysubs"
REPLY_UNSUBSCRIBED: Final = "Unsubscribed."
REPLY_NO_SUCH_SUB: Final = "No subscription with that id is yours."


@dataclass(eq=False)
class DiscordGateway:
    """Neutral-service logic behind the Discord event and command shell.

    Capture-only deployments leave ``subscriptions`` unset; deployments
    without an archive leave ``capture``/``masker`` unset. Each method fails
    closed to a helpful reply string when its feature is off.
    """

    directory: ChannelDirectory
    groups: GroupPolicy
    capture: CaptureService | None = None
    masker: DiscordAuthorMasker | None = None
    subscriptions: SubscriptionStore | None = None
    default_lang: str = "en"

    async def ingest_message(  # noqa: PLR0913 -- the flattened message facts
        self,
        *,
        channel_id: int,
        thread_id: int | None,
        author_id: int,
        message_id: int,
        posted_at: datetime,
        text: str,
        reply_to: int | None,
    ) -> None:
        """Archive one message iff capture is on for its scope (never raises)."""
        capture, masker = self.capture, self.masker
        if capture is None or masker is None:
            return
        scope = scope_of(channel_id, thread_id)
        if not self.groups.is_allowed(scope):
            return
        # Pseudonymize the author here, at the boundary, before the message
        # crosses into the neutral services (spec R6). CaptureService applies
        # the per-scope truncation from DISCORD_CAPABILITIES.max_message_chars.
        author = masker.mask(channel_id, thread_id or 0, author_id)
        await capture.ingest(
            CapturedMessage(
                scope=scope,
                message_id=message_id,
                posted_at=posted_at,
                author=author,
                kind="text" if text else "media_note",
                text=text,
                reply_to=reply_to,
            )
        )

    async def capture_command(
        self, channel_id: int, thread_id: int | None, *, enabled: bool, is_admin: bool
    ) -> str:
        """Turn this channel's message capture on or off (server admins only)."""
        if not is_admin:
            return REPLY_NOT_ADMIN
        capture = self.capture
        if capture is None:
            return REPLY_CAPTURE_OFF_DEPLOY
        scope = scope_of(channel_id, thread_id)
        if not self.groups.is_allowed(scope):
            return REPLY_NOT_ALLOWED
        try:
            await self.directory.set_capture(scope, enabled=enabled)
            capture.forget_scope(scope)
        except StorageError:
            if not enabled:
                # The durable disable never landed: tombstone the scope so
                # the maintenance tick converges the revocation instead of
                # resuming off the stale row (mirrors the Telegram path).
                capture.deny_scope(scope)
            return REPLY_STORAGE_DOWN
        return REPLY_CAPTURE_ENABLED if enabled else REPLY_CAPTURE_DISABLED

    async def setpage_command(
        self, channel_id: int, thread_id: int | None, page: str, *, is_admin: bool
    ) -> str:
        """Point this channel's analyses at a wiki page (server admins only)."""
        if not is_admin:
            return REPLY_NOT_ADMIN
        title = page.strip()
        if not title:
            return REPLY_SETPAGE_USAGE
        scope = scope_of(channel_id, thread_id)
        try:
            normalized = await self.directory.set_log_page(scope, title)
        except PageNotAllowedError:
            return REPLY_PAGE_REFUSED
        except SelfServiceUnavailableError:
            return REPLY_SELF_SERVICE_OFF
        except StorageError:
            return REPLY_STORAGE_DOWN
        log_event("profile_update", "ok")
        return REPLY_PAGE_SET.format(page=normalized)

    async def subscribe_command(
        self, channel_id: int, thread_id: int | None, dm_channel_id: int, options: str
    ) -> str:
        """Create a durable DM digest subscription to this channel.

        Discord has no deep links, so a channel becomes subscribable the
        first time anyone subscribes to it — minting the ``subscribe_code``
        the neutral scheduler requires — rather than via an admin-shared
        link.
        """
        subscriptions = self.subscriptions
        if subscriptions is None:
            return REPLY_SUBS_UNAVAILABLE
        try:
            schedule, recipe, lang = parse_subscription(options, self.default_lang)
        except SubscriptionParseError as error:
            return str(error)
        scope = scope_of(channel_id, thread_id)
        subscription = Subscription(
            sub_id=mint_sub_id(),
            dm=dm_scope(dm_channel_id),
            scope=scope,
            schedule=schedule,
            recipe=recipe,
            lang=lang,
        )
        try:
            await self._ensure_subscribable(scope)
            await subscriptions.add(subscription)
        except StorageError:
            return REPLY_STORAGE_DOWN
        log_event("subscription_add", "ok")
        return REPLY_SUBSCRIBED.format(
            sub_id=subscription.sub_id, schedule=schedule.token, recipe=recipe, lang=lang
        )

    async def mysubs_command(self, dm_channel_id: int) -> str:
        """List the caller's DM digest subscriptions."""
        subscriptions = self.subscriptions
        if subscriptions is None:
            return REPLY_SUBS_UNAVAILABLE
        dm = dm_scope(dm_channel_id)
        try:
            subs = await subscriptions.list_for_user(dm)
        except StorageError:
            return REPLY_STORAGE_DOWN
        if not subs:
            return REPLY_NO_SUBS
        lines = [REPLY_SUBS_HEADER]
        lines += [f"[{s.sub_id}] {s.schedule.token} {s.recipe} ({s.lang})" for s in subs]
        return "\n".join(lines)

    async def unsubscribe_command(self, dm_channel_id: int, subscription_id: str) -> str:
        """Remove one of the caller's subscriptions by id."""
        subscriptions = self.subscriptions
        if subscriptions is None:
            return REPLY_SUBS_UNAVAILABLE
        sub_id = subscription_id.strip()
        if not sub_id:
            return REPLY_UNSUB_USAGE
        try:
            removed = await subscriptions.remove(dm_scope(dm_channel_id), sub_id)
        except StorageError:
            return REPLY_STORAGE_DOWN
        return REPLY_UNSUBSCRIBED if removed else REPLY_NO_SUCH_SUB

    async def _ensure_subscribable(self, scope: Scope) -> None:
        profile = await self.directory.profile_of(scope)
        if profile.subscribe_code is None:
            await self.directory.set_subscribe_code(scope, mint_subscribe_code())


def _channel_ids(channel: Any) -> tuple[int, int | None]:
    """Split a Discord channel into ``(parent_channel_id, thread_id | None)``.

    A thread carries ``parent_id`` (the enclosing channel) and its own id;
    a plain channel has no ``parent_id``, so it is the target itself.
    """
    parent_id = getattr(channel, "parent_id", None)
    if parent_id is not None:
        return parent_id, channel.id
    return channel.id, None


def _is_admin(user: Any) -> bool:
    """Whether the invoking user administers the server (checked live, never stored)."""
    perms = getattr(user, "guild_permissions", None)
    return bool(perms is not None and perms.administrator)


async def _respond(interaction: discord.Interaction, text: str) -> None:
    """Answer a slash command ephemerally (only the caller sees it)."""
    await interaction.response.send_message(text, ephemeral=True)


def default_intents() -> discord.Intents:
    """The gateway intents: message content (privileged) + members."""
    intents = discord.Intents.default()
    intents.message_content = True
    intents.members = True
    return intents


class DiscordGatewayClient(discord.Client):
    """Thin ``discord.Client`` shell: forwards SDK events to :class:`DiscordGateway`.

    Holds no business logic — ``on_message`` and each slash command pull the
    plain values off the SDK objects and delegate. ``setup_hook`` publishes
    the slash commands and runs the injected startup hook (storage bootstrap
    + the background delivery loops).
    """

    def __init__(
        self,
        gateway: DiscordGateway,
        *,
        intents: discord.Intents,
        on_setup: Callable[[DiscordGatewayClient], Awaitable[None]] | None = None,
    ) -> None:
        super().__init__(intents=intents)
        self._gateway = gateway
        self._on_setup = on_setup
        self.tree = app_commands.CommandTree(self)
        self._register_commands()

    async def setup_hook(self) -> None:
        """Publish slash commands, then run the injected startup hook."""
        await self.tree.sync()
        if self._on_setup is not None:
            await self._on_setup(self)

    async def on_message(self, message: discord.Message) -> None:
        """Feed one incoming message to capture ingestion (bots ignored)."""
        if message.author.bot:
            return
        channel_id, thread_id = _channel_ids(message.channel)
        reference = message.reference
        await self._gateway.ingest_message(
            channel_id=channel_id,
            thread_id=thread_id,
            author_id=message.author.id,
            message_id=message.id,
            posted_at=message.created_at,
            text=message.content,
            reply_to=reference.message_id if reference is not None else None,
        )

    def _register_commands(self) -> None:
        gateway = self._gateway

        @self.tree.command(name="capture", description="Turn message capture on/off (admins).")
        @app_commands.guild_only()
        @app_commands.describe(state="on or off")
        async def capture(interaction: discord.Interaction, state: str) -> None:
            channel_id, thread_id = _channel_ids(interaction.channel)
            reply = await gateway.capture_command(
                channel_id,
                thread_id,
                enabled=state.strip().lower() == "on",
                is_admin=_is_admin(interaction.user),
            )
            await _respond(interaction, reply)

        @self.tree.command(name="setpage", description="Set this channel's wiki page (admins).")
        @app_commands.guild_only()
        @app_commands.describe(page="Wiki page path")
        async def setpage(interaction: discord.Interaction, page: str) -> None:
            channel_id, thread_id = _channel_ids(interaction.channel)
            reply = await gateway.setpage_command(
                channel_id, thread_id, page, is_admin=_is_admin(interaction.user)
            )
            await _respond(interaction, reply)

        @self.tree.command(name="subscribe", description="Get this channel's digest in your DMs.")
        @app_commands.guild_only()
        @app_commands.describe(options="schedule / recipe / lang, e.g. daily@08:00 summarize")
        async def subscribe(interaction: discord.Interaction, options: str = "") -> None:
            channel_id, thread_id = _channel_ids(interaction.channel)
            dm = await interaction.user.create_dm()
            reply = await gateway.subscribe_command(channel_id, thread_id, dm.id, options)
            await _respond(interaction, reply)

        @self.tree.command(name="mysubs", description="List your digest subscriptions.")
        async def mysubs(interaction: discord.Interaction) -> None:
            dm = await interaction.user.create_dm()
            await _respond(interaction, await gateway.mysubs_command(dm.id))

        @self.tree.command(name="unsubscribe", description="Stop one of your digest subscriptions.")
        @app_commands.describe(subscription_id="The id shown by /mysubs")
        async def unsubscribe(interaction: discord.Interaction, subscription_id: str) -> None:
            dm = await interaction.user.create_dm()
            await _respond(interaction, await gateway.unsubscribe_command(dm.id, subscription_id))


def build_gateway_client(
    gateway: DiscordGateway,
    *,
    on_setup: Callable[[DiscordGatewayClient], Awaitable[None]] | None = None,
    intents: discord.Intents | None = None,
) -> DiscordGatewayClient:
    """Build the gateway client with the privileged intents by default."""
    # An explicit ``Intents`` is used verbatim even when it is empty
    # (``Intents.none()`` is falsy, so ``or`` would wrongly discard it).
    resolved = default_intents() if intents is None else intents
    return DiscordGatewayClient(gateway, intents=resolved, on_setup=on_setup)


def run(client: discord.Client, token: str) -> None:
    """Start the gateway; blocks for the process lifetime (the network edge)."""
    client.run(token)
