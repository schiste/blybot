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
``/capture``, ``/setpage``, ``/settings``, ``/reset``, ``/revoke``,
``/llm``, ``/setrepo``, ``/settoken``, ``/events``, ``/rule``, ``/rules``
and ``/action`` are server-admin config; ``/issue`` and ``/repo`` are open to
any member; ``/subscribe``, ``/mysubs`` and ``/unsubscribe`` are the
durable-DM digest flow. Every reply is ephemeral — for ``/issue`` that is
load-bearing, not cosmetic: a public response would attribute the command
to its caller and defeat the anonymity it promises.

``/settoken`` opens a :class:`TokenModal` rather than taking the secret as
a slash-command parameter — see that class for why. ``/rule`` and
``/action`` are each an
:class:`~discord.app_commands.Group`, so Discord routes its subcommands
natively and each leaf is one neutral call — the grammar itself still lives
in the shared :class:`~blybot.services.commands.CommandService`. Outbound
digests, reminders and repo notifications go out through the shared neutral
delivery loop driving
:class:`~blybot.adapters.discord.transport.DiscordTransport`.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Final

import discord
from discord import app_commands

from blybot.adapters.discord.scope import dm_scope, scope_of
from blybot.domain.models import CapturedMessage, CommandResult, LogContent
from blybot.domain.ports import StorageError, WikiWriteError
from blybot.services import commands as cmd
from blybot.services.subscriptions import (
    MAX_SUBS_PER_USER,
    mint_subscribe_code,
)
from blybot.services.transcribe import record_dm_line

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable
    from datetime import datetime

    from blybot.domain.models import Scope
    from blybot.domain.ports import SubscriptionStore
    from blybot.services.analysis_run import AnalysisService
    from blybot.services.capture import CaptureService
    from blybot.services.commands import CommandService
    from blybot.services.directory import ChannelDirectory
    from blybot.services.dm_routing import DmRouteRegistry
    from blybot.services.policy import GroupPolicy
    from blybot.services.transcribe import DmTranscriptionService

    from .author_mask import DiscordAuthorMasker

# Capture and setpage now render whatever the neutral CommandService returns;
# their wording lives in blybot.services.commands. The strings that remain here
# belong to commands this adapter still owns end to end (the DM digest flow).
REPLY_STORAGE_DOWN: Final = "That's temporarily unavailable — please try again later."
# Appended to a successful /setrepo: Discord's own way of collecting the
# secret. Telegram appends a deep link at the same point instead.
REPLY_PAT_NEXT_STEP: Final = (
    "Run /settoken here to supply the GitHub token — it opens a private form, "
    "so the secret is never posted as a message. Use a fine-grained PAT "
    "restricted to that repository with Issues read/write only."
)
PAT_MODAL_TITLE: Final = "GitHub token"
PAT_MODAL_LABEL: Final = "Fine-grained PAT (Issues read/write)"
# The message context-menu entry label (Apps → …).
LOG_MENU_LABEL: Final = "Log to wiki"
REPLY_TRANSCRIBE_UNAVAILABLE: Final = "Private transcription isn't available on this deployment."
REPLY_TRANSCRIBE_OPENED: Final = (
    "Check your DMs — everything you send me there now publishes to {page} as one "
    "discussion, under a pseudonym that is never derived from your account. Send as "
    "many messages as you like; run /transcribe again in another channel to switch, "
    "or just stop writing."
)
REPLY_TRANSCRIBE_SESSION: Final = (
    "You appear as {pseudonym} on {page}. Everything you send here from now on joins "
    "that same discussion until the session times out."
)
REPLY_TRANSCRIBE_FAILED: Final = (
    "Sorry, publishing failed. The operator can see details in the logs."
)
REPLY_ANALYSES_UNAVAILABLE: Final = "On-demand analyses aren't available on this deployment."
REPLY_SUBS_UNAVAILABLE: Final = "Digest subscriptions aren't available on this deployment."


@dataclass(eq=False)
class DiscordGateway:
    """Neutral-service logic behind the Discord event and command shell.

    Capture-only deployments leave ``subscriptions`` unset; deployments
    without an archive leave ``capture``/``masker`` unset. Each method fails
    closed to a helpful reply string when its feature is off.
    """

    directory: ChannelDirectory
    groups: GroupPolicy
    commands: CommandService
    capture: CaptureService | None = None
    masker: DiscordAuthorMasker | None = None
    subscriptions: SubscriptionStore | None = None
    analysis: AnalysisService | None = None
    # DM transcription (#45): both unset on a deployment without it.
    transcription: DmTranscriptionService | None = None
    routes: DmRouteRegistry | None = None
    default_lang: str = "en"
    # Per-subscriber ceiling enforced at /subscribe admission (#23).
    max_subs_per_user: int = MAX_SUBS_PER_USER

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
    ) -> CommandResult:
        """Turn this channel's message capture on or off (server admins only).

        Returns the whole :class:`CommandResult`, not just its text — unlike
        every other command here — because the shell must know whether this
        succeeded. Discord has no privacy mode, so the ON confirmation *is*
        the channel's only notice that archiving started, and it has to be
        posted publicly and permanently rather than ephemerally to the admin
        who ran it (issue #17). A refusal still answers privately.
        """
        return await self.commands.capture(
            scope_of(channel_id, thread_id), is_admin=is_admin, enabled=enabled
        )

    async def setpage_command(
        self, channel_id: int, thread_id: int | None, page: str, *, is_admin: bool
    ) -> str:
        """Point this channel's analyses at a wiki page (server admins only)."""
        result = await self.commands.set_page(
            scope_of(channel_id, thread_id), is_admin=is_admin, page=page
        )
        return result.text

    async def settings_command(
        self, channel_id: int, thread_id: int | None, *, is_admin: bool
    ) -> str:
        """Show this channel's effective configuration (server admins only)."""
        result = await self.commands.show_settings(
            scope_of(channel_id, thread_id), is_admin=is_admin
        )
        return result.text

    async def reset_command(self, channel_id: int, thread_id: int | None, *, is_admin: bool) -> str:
        """Forget this channel's profile, returning it to defaults (server admins only)."""
        result = await self.commands.reset(scope_of(channel_id, thread_id), is_admin=is_admin)
        return result.text

    async def revoke_command(
        self, channel_id: int, thread_id: int | None, *, is_admin: bool
    ) -> str:
        """Discard this channel's stored API token (server admins only)."""
        result = await self.commands.revoke_token(
            scope_of(channel_id, thread_id), is_admin=is_admin
        )
        return result.text

    async def llm_command(
        self, channel_id: int, thread_id: int | None, options: str, *, is_admin: bool
    ) -> str:
        """Show, set, or reset this channel's LLM settings (server admins only)."""
        result = await self.commands.set_llm(
            scope_of(channel_id, thread_id), is_admin=is_admin, tokens=options.split()
        )
        return result.text

    async def setrepo_command(
        self, channel_id: int, thread_id: int | None, repo: str, *, is_admin: bool
    ) -> str:
        """Bind this channel to a GitHub repository (server admins only)."""
        result = await self.commands.set_repo(
            scope_of(channel_id, thread_id), is_admin=is_admin, repo=repo
        )
        # The token is collected separately, through the /settoken modal —
        # Discord has no deep link, and a slash-command parameter would put
        # the secret in the visible command bar and the client's history.
        return f"{result.text} {REPLY_PAT_NEXT_STEP}" if result.ok else result.text

    async def settoken_command(
        self, channel_id: int, thread_id: int | None, token: str, *, is_admin: bool
    ) -> str:
        """Store the GitHub token submitted through the modal (server admins only).

        The secret arrives in the interaction payload and is never posted as
        a message anywhere, so — unlike Telegram — there is nothing to delete
        afterwards. Validation and encryption are the neutral service's.
        """
        result = await self.commands.store_token(
            scope_of(channel_id, thread_id), is_admin=is_admin, token=token
        )
        return result.text

    async def issue_command(self, channel_id: int, thread_id: int | None, description: str) -> str:
        """File an anonymous issue in this channel's bound repo (any member).

        Not admin-gated by design. The reply MUST stay ephemeral: a public
        slash-command response attributes the invocation to its caller in
        the channel, which would defeat the anonymity the command promises.
        """
        result = await self.commands.file_issue(
            scope_of(channel_id, thread_id), description=description
        )
        return result.text

    async def repo_command(self, channel_id: int, thread_id: int | None) -> str:
        """Show the bound repository's open-items summary (any member)."""
        result = await self.commands.repo_summary(scope_of(channel_id, thread_id))
        return result.text

    async def transcribe_command(self, channel_id: int, thread_id: int | None, dm_id: int) -> str:
        """Open a DM that publishes to this channel's wiki page (any member).

        Discord's answer to Telegram's deep-link-plus-picker dance: because the
        bot may open the DM itself (``bot_can_open_dm``), the flow starts *in
        the target channel*, so the destination is known before the DM exists
        and there is nothing to pick. The same page guard as ``/log`` applies —
        a scope that never chose a page must not publish onto the operator
        default.
        """
        transcription, routes = self.transcription, self.routes
        if transcription is None or routes is None:
            return REPLY_TRANSCRIBE_UNAVAILABLE
        scope = scope_of(channel_id, thread_id)
        if not self.groups.is_allowed(scope):
            return cmd.REPLY_NOT_ALLOWED
        settings = await self.directory.resolve(scope)
        if settings.degraded:
            return cmd.REPLY_LOG_CONFIG_UNAVAILABLE
        if self.directory.self_service_enabled and not settings.page_explicit:
            return cmd.REPLY_LOG_NO_PAGE
        routes.save_route(dm_scope(dm_id), scope, settings.log_page)
        return REPLY_TRANSCRIBE_OPENED.format(page=settings.log_page)

    async def transcribe_dm(self, dm_id: int, text: str) -> str | None:
        """Transcribe one DM line to its routed page; ``None`` if unrouted.

        Returning ``None`` rather than a complaint keeps an unrouted DM silent:
        the bot must not respond to private messages it was never asked to
        publish.
        """
        transcription, routes = self.transcription, self.routes
        if transcription is None or routes is None:
            return None
        dm = dm_scope(dm_id)
        route = routes.route_for(dm)
        if route is None:
            return None
        try:
            session, opened = await record_dm_line(transcription, routes, dm, text, route.page)
        except WikiWriteError:
            return REPLY_TRANSCRIBE_FAILED
        if not opened:
            return None  # stay quiet mid-conversation
        return REPLY_TRANSCRIBE_SESSION.format(pseudonym=session.pseudonym, page=route.page)

    async def log_command(
        self,
        channel_id: int,
        thread_id: int | None,
        *,
        text: str,
        is_author: bool,
    ) -> str:
        """Publish one message to this channel's wiki page, unattributed.

        ``is_author`` is resolved in the shell by comparing the invoker with
        the target's author — raw Discord identities never leave this adapter.
        Discord needs no requester-hiding step: a context-menu command posts
        no message at all, so there is nothing to delete (where Telegram must
        race to remove the ``/log`` command message).
        """
        result = await self.commands.log_message(
            scope_of(channel_id, thread_id), is_author=is_author, content=LogContent(text=text)
        )
        return result.text

    async def action_add_command(
        self, channel_id: int, thread_id: int | None, spec: str, *, is_admin: bool
    ) -> str:
        """Schedule one recurring analysis for this channel (server admins only)."""
        result = await self.commands.add_action(
            scope_of(channel_id, thread_id), is_admin=is_admin, spec=spec
        )
        return result.text

    async def action_remove_command(
        self, channel_id: int, thread_id: int | None, action_id: str, *, is_admin: bool
    ) -> str:
        """Drop one of this channel's scheduled analyses (server admins only)."""
        result = await self.commands.remove_action(
            scope_of(channel_id, thread_id), is_admin=is_admin, action_id=action_id
        )
        return result.text

    async def action_list_command(
        self, channel_id: int, thread_id: int | None, *, is_admin: bool
    ) -> str:
        """List this channel's scheduled analyses (server admins only)."""
        result = await self.commands.list_actions(
            scope_of(channel_id, thread_id), is_admin=is_admin
        )
        return result.text

    async def events_command(
        self, channel_id: int, thread_id: int | None, state: str, *, is_admin: bool
    ) -> str:
        """Turn this channel's repo notifications on or off (server admins only)."""
        result = await self.commands.events(
            scope_of(channel_id, thread_id), is_admin=is_admin, tokens=state.split()
        )
        return result.text

    async def rule_add_command(
        self, channel_id: int, thread_id: int | None, spec: str, *, is_admin: bool
    ) -> str:
        """Add one composable repo-event rule to this channel (server admins only)."""
        result = await self.commands.add_rule(
            scope_of(channel_id, thread_id), is_admin=is_admin, spec=spec
        )
        return result.text

    async def rule_remove_command(
        self, channel_id: int, thread_id: int | None, rule_id: str, *, is_admin: bool
    ) -> str:
        """Drop one of this channel's rules by id (server admins only)."""
        result = await self.commands.remove_rule(
            scope_of(channel_id, thread_id), is_admin=is_admin, rule_id=rule_id
        )
        return result.text

    async def rule_clear_command(
        self, channel_id: int, thread_id: int | None, *, is_admin: bool
    ) -> str:
        """Remove every rule from this channel (server admins only)."""
        result = await self.commands.clear_rules(scope_of(channel_id, thread_id), is_admin=is_admin)
        return result.text

    async def rules_command(self, channel_id: int, thread_id: int | None, *, is_admin: bool) -> str:
        """List this channel's repo-event rules with their ids (server admins only)."""
        result = await self.commands.list_rules(scope_of(channel_id, thread_id), is_admin=is_admin)
        return result.text

    async def analyze_command(
        self,
        channel_id: int,
        thread_id: int | None,
        *,
        command: str,
        recipe: str,
        is_admin: bool,
    ) -> str:
        """Run one on-demand analysis and publish it to the channel's wiki page.

        A thin map to the neutral :class:`AnalysisService`: pull the scope off
        the Discord ids, delegate, and render the reply. The slash-command
        shell defers the interaction before calling this — a chunked analysis
        runs well past Discord's 3-second acknowledgement deadline.
        """
        analysis = self.analysis
        if analysis is None:
            return REPLY_ANALYSES_UNAVAILABLE
        result = await analysis.run_analysis(
            scope_of(channel_id, thread_id),
            is_admin=is_admin,
            command=command,
            recipe=recipe,
            tokens=[],
        )
        return result.text

    async def subscribe_command(
        self, channel_id: int, thread_id: int | None, dm_channel_id: int, options: str
    ) -> str:
        """Create a durable DM digest subscription to this channel.

        Discord has no deep links, so a channel becomes subscribable the first
        time anyone subscribes to it — minting the ``subscribe_code`` the
        neutral scheduler requires — rather than via an admin-shared link.
        That mint is the only Discord-specific step; everything after it is
        the shared :class:`CommandService`.
        """
        if self.commands.subscriptions is None:
            return REPLY_SUBS_UNAVAILABLE
        scope = scope_of(channel_id, thread_id)
        try:
            await self._ensure_subscribable(scope)
        except StorageError:
            return REPLY_STORAGE_DOWN
        result = await self.commands.subscribe(scope, dm_scope(dm_channel_id), options=options)
        return result.text

    async def mysubs_command(self, dm_channel_id: int) -> str:
        """List the caller's DM digest subscriptions."""
        result = await self.commands.list_subscriptions(dm_scope(dm_channel_id))
        return result.text

    async def unsubscribe_command(self, dm_channel_id: int, subscription_id: str) -> str:
        """Remove one of the caller's subscriptions by id."""
        result = await self.commands.unsubscribe(
            dm_scope(dm_channel_id), subscription_id=subscription_id
        )
        return result.text

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


async def _respond(interaction: discord.Interaction, text: str, *, ephemeral: bool = True) -> None:
    """Answer a slash command; ephemeral (caller-only) unless told otherwise.

    Ephemeral is the default because it is usually the privacy-preserving
    choice — a public response attributes the invocation to its caller, which
    would deanonymize ``/issue`` and the log context menu. ``/capture on`` is
    the deliberate exception: there the *channel* is the audience.
    """
    await interaction.response.send_message(text, ephemeral=ephemeral)


class TokenModal(discord.ui.Modal):
    """The private form carrying a GitHub token to :meth:`DiscordGateway.settoken_command`.

    Discord has no deep link, so this replaces Telegram's DM-paste flow. A
    modal's field value travels in the interaction payload and is never
    posted as a message — not to the channel, not to a DM — so unlike
    Telegram there is no pasted secret to race to delete, and nothing lands
    in any chat history. Admin-ship is re-checked here at *submit* time, not
    only when the form was opened.
    """

    def __init__(self, gateway: DiscordGateway, channel_id: int, thread_id: int | None) -> None:
        super().__init__(title=PAT_MODAL_TITLE)
        self._gateway = gateway
        self._channel_id = channel_id
        self._thread_id = thread_id
        self.token_input: discord.ui.TextInput[TokenModal] = discord.ui.TextInput(
            label=PAT_MODAL_LABEL, required=True, max_length=255
        )
        self.add_item(self.token_input)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        """Hand the submitted secret to the neutral service and answer privately."""
        reply = await self._gateway.settoken_command(
            self._channel_id,
            self._thread_id,
            self.token_input.value,
            is_admin=_is_admin(interaction.user),
        )
        await _respond(interaction, reply)


def default_intents() -> discord.Intents:
    """The gateway intents: message content (the one privileged intent capture needs).

    Members/presence are deliberately NOT requested — join detection is not
    wired on Discord, so keeping to the single privileged intent is least
    privilege and means an operator only has to enable Message Content in the
    Developer Portal.
    """
    intents = discord.Intents.default()
    intents.message_content = True
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
        """Route one incoming message: DM transcription, else capture ingestion.

        A DM is never a capture candidate — capture is a per-channel, admin-
        announced thing — so the two paths are exclusive.
        """
        if message.author.bot:
            return
        if message.guild is None:
            notice = await self._gateway.transcribe_dm(message.channel.id, message.content)
            if notice is not None:
                await message.channel.send(notice)
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

    def _register_commands(self) -> None:  # noqa: PLR0915 -- one flat block per slash command
        gateway = self._gateway

        @self.tree.command(name="capture", description="Turn message capture on/off (admins).")
        @app_commands.guild_only()
        @app_commands.describe(state="on or off")
        async def capture(interaction: discord.Interaction, state: str) -> None:
            channel_id, thread_id = _channel_ids(interaction.channel)
            result = await gateway.capture_command(
                channel_id,
                thread_id,
                enabled=state.strip().lower() == "on",
                is_admin=_is_admin(interaction.user),
            )
            # The ONE command whose success must be public: on a platform with
            # no privacy mode this reply is the channel's notice that its
            # messages are being archived. Refusals ("not an admin", storage
            # down) stay ephemeral — no reason to broadcast those.
            await _respond(interaction, result.text, ephemeral=not result.ok)

        @self.tree.command(name="setpage", description="Set this channel's wiki page (admins).")
        @app_commands.guild_only()
        @app_commands.describe(page="Wiki page path")
        async def setpage(interaction: discord.Interaction, page: str) -> None:
            channel_id, thread_id = _channel_ids(interaction.channel)
            reply = await gateway.setpage_command(
                channel_id, thread_id, page, is_admin=_is_admin(interaction.user)
            )
            await _respond(interaction, reply)

        @self.tree.command(
            name="settings", description="Show this channel's configuration (admins)."
        )
        @app_commands.guild_only()
        async def settings(interaction: discord.Interaction) -> None:
            channel_id, thread_id = _channel_ids(interaction.channel)
            reply = await gateway.settings_command(
                channel_id, thread_id, is_admin=_is_admin(interaction.user)
            )
            await _respond(interaction, reply)

        @self.tree.command(
            name="reset", description="Forget this channel's configuration (admins)."
        )
        @app_commands.guild_only()
        async def reset(interaction: discord.Interaction) -> None:
            channel_id, thread_id = _channel_ids(interaction.channel)
            reply = await gateway.reset_command(
                channel_id, thread_id, is_admin=_is_admin(interaction.user)
            )
            await _respond(interaction, reply)

        @self.tree.command(
            name="revoke", description="Discard this channel's stored token (admins)."
        )
        @app_commands.guild_only()
        async def revoke(interaction: discord.Interaction) -> None:
            channel_id, thread_id = _channel_ids(interaction.channel)
            reply = await gateway.revoke_command(
                channel_id, thread_id, is_admin=_is_admin(interaction.user)
            )
            await _respond(interaction, reply)

        @self.tree.command(
            name="llm", description="Show/set/reset this channel's LLM settings (admins)."
        )
        @app_commands.guild_only()
        @app_commands.describe(options="show · set key:value … · reset")
        async def llm(interaction: discord.Interaction, options: str = "") -> None:
            channel_id, thread_id = _channel_ids(interaction.channel)
            reply = await gateway.llm_command(
                channel_id, thread_id, options, is_admin=_is_admin(interaction.user)
            )
            await _respond(interaction, reply)

        # A message context-menu entry, not a slash command: Discord's
        # equivalent of Telegram's "reply to a message with /log". Right-click
        # the message → Apps → Log to wiki.
        async def log_to_wiki(interaction: discord.Interaction, message: discord.Message) -> None:
            channel_id, thread_id = _channel_ids(interaction.channel)
            reply = await gateway.log_command(
                channel_id,
                thread_id,
                text=message.content,
                # Compared HERE, at the boundary: the neutral service is told
                # only whether the requester authored the target.
                is_author=interaction.user.id == message.author.id,
            )
            # Ephemeral keeps the requester unattributed in the channel, the
            # property Telegram gets by deleting the command message.
            await _respond(interaction, reply)

        self.tree.add_command(app_commands.ContextMenu(name=LOG_MENU_LABEL, callback=log_to_wiki))

        @self.tree.command(
            name="transcribe", description="Publish a private discussion to this channel's page."
        )
        @app_commands.guild_only()
        async def transcribe(interaction: discord.Interaction) -> None:
            channel_id, thread_id = _channel_ids(interaction.channel)
            dm = await interaction.user.create_dm()
            reply = await gateway.transcribe_command(channel_id, thread_id, dm.id)
            # Ephemeral: which channel someone is about to write privately about
            # is nobody else's business.
            await _respond(interaction, reply)

        @self.tree.command(name="issue", description="File an anonymous issue in the bound repo.")
        @app_commands.guild_only()
        @app_commands.describe(description="What is wrong")
        async def issue(interaction: discord.Interaction, description: str) -> None:
            channel_id, thread_id = _channel_ids(interaction.channel)
            reply = await gateway.issue_command(channel_id, thread_id, description)
            # Ephemeral is load-bearing here, not just tidy: a public response
            # would show "<user> used /issue" in the channel and deanonymize
            # the reporter the command exists to protect.
            await _respond(interaction, reply)

        @self.tree.command(name="repo", description="Show the bound repo's open items.")
        @app_commands.guild_only()
        async def repo(interaction: discord.Interaction) -> None:
            channel_id, thread_id = _channel_ids(interaction.channel)
            await _respond(interaction, await gateway.repo_command(channel_id, thread_id))

        @self.tree.command(name="setrepo", description="Bind this channel to a GitHub repo.")
        @app_commands.guild_only()
        @app_commands.describe(repo="owner/repository")
        async def setrepo(interaction: discord.Interaction, repo: str) -> None:
            channel_id, thread_id = _channel_ids(interaction.channel)
            reply = await gateway.setrepo_command(
                channel_id, thread_id, repo, is_admin=_is_admin(interaction.user)
            )
            await _respond(interaction, reply)

        @self.tree.command(
            name="settoken", description="Supply this channel's GitHub token privately (admins)."
        )
        @app_commands.guild_only()
        async def settoken(interaction: discord.Interaction) -> None:
            # A modal MUST be the first response to the interaction — it can
            # neither follow a defer nor be sent as a follow-up — so nothing
            # awaitable may run before send_modal. Refuse non-admins here
            # rather than showing them the form; TokenModal re-checks on submit.
            if not _is_admin(interaction.user):
                await _respond(interaction, cmd.REPLY_NOT_ADMIN)
                return
            channel_id, thread_id = _channel_ids(interaction.channel)
            await interaction.response.send_modal(TokenModal(gateway, channel_id, thread_id))

        # /action mirrors /rule: Discord routes the subcommands natively, so
        # each leaf is one neutral call and the grammar stays in the service.
        action = app_commands.Group(
            name="action",
            description="Schedule recurring analyses for this channel (admins).",
            guild_only=True,
        )
        self.tree.add_command(action)

        @action.command(name="add", description="Schedule a recurring analysis.")
        @app_commands.describe(spec="e.g. daily@06:00 summarize window=24h")
        async def action_add(interaction: discord.Interaction, spec: str) -> None:
            channel_id, thread_id = _channel_ids(interaction.channel)
            reply = await gateway.action_add_command(
                channel_id, thread_id, spec, is_admin=_is_admin(interaction.user)
            )
            await _respond(interaction, reply)

        @action.command(name="remove", description="Remove a scheduled analysis by id.")
        @app_commands.describe(action_id="The id shown by /action list")
        async def action_remove(interaction: discord.Interaction, action_id: str) -> None:
            channel_id, thread_id = _channel_ids(interaction.channel)
            reply = await gateway.action_remove_command(
                channel_id, thread_id, action_id, is_admin=_is_admin(interaction.user)
            )
            await _respond(interaction, reply)

        @action.command(name="list", description="List this channel's scheduled analyses.")
        async def action_list(interaction: discord.Interaction) -> None:
            channel_id, thread_id = _channel_ids(interaction.channel)
            reply = await gateway.action_list_command(
                channel_id, thread_id, is_admin=_is_admin(interaction.user)
            )
            await _respond(interaction, reply)

        @self.tree.command(
            name="events", description="Turn rule-driven repo notifications on/off (admins)."
        )
        @app_commands.guild_only()
        @app_commands.describe(state="on or off")
        async def events(interaction: discord.Interaction, state: str) -> None:
            channel_id, thread_id = _channel_ids(interaction.channel)
            reply = await gateway.events_command(
                channel_id, thread_id, state, is_admin=_is_admin(interaction.user)
            )
            await _respond(interaction, reply)

        @self.tree.command(name="rules", description="List this channel's event rules (admins).")
        @app_commands.guild_only()
        async def rules(interaction: discord.Interaction) -> None:
            channel_id, thread_id = _channel_ids(interaction.channel)
            reply = await gateway.rules_command(
                channel_id, thread_id, is_admin=_is_admin(interaction.user)
            )
            await _respond(interaction, reply)

        # Discord routes subcommands natively, so /rule needs no argv
        # dispatcher of its own — each leaf calls the matching neutral
        # CommandService method directly.
        rule = app_commands.Group(
            name="rule",
            description="Manage this channel's repo event rules (admins).",
            guild_only=True,
        )
        self.tree.add_command(rule)

        @rule.command(name="add", description="Add an event rule.")
        @app_commands.describe(spec="e.g. pr.merged base:main digest")
        async def rule_add(interaction: discord.Interaction, spec: str) -> None:
            channel_id, thread_id = _channel_ids(interaction.channel)
            reply = await gateway.rule_add_command(
                channel_id, thread_id, spec, is_admin=_is_admin(interaction.user)
            )
            await _respond(interaction, reply)

        @rule.command(name="remove", description="Remove an event rule by id.")
        @app_commands.describe(rule_id="The id shown by /rules")
        async def rule_remove(interaction: discord.Interaction, rule_id: str) -> None:
            channel_id, thread_id = _channel_ids(interaction.channel)
            reply = await gateway.rule_remove_command(
                channel_id, thread_id, rule_id, is_admin=_is_admin(interaction.user)
            )
            await _respond(interaction, reply)

        @rule.command(name="clear", description="Remove every event rule here.")
        async def rule_clear(interaction: discord.Interaction) -> None:
            channel_id, thread_id = _channel_ids(interaction.channel)
            reply = await gateway.rule_clear_command(
                channel_id, thread_id, is_admin=_is_admin(interaction.user)
            )
            await _respond(interaction, reply)

        async def run_analysis(interaction: discord.Interaction, command: str, recipe: str) -> None:
            # CRITICAL: acknowledge FIRST. A chunked analysis runs for minutes,
            # far past Discord's 3-second interaction deadline; deferring keeps
            # the interaction alive so the result can arrive as a follow-up.
            await interaction.response.defer()
            channel_id, thread_id = _channel_ids(interaction.channel)
            reply = await gateway.analyze_command(
                channel_id,
                thread_id,
                command=command,
                recipe=recipe,
                is_admin=_is_admin(interaction.user),
            )
            await interaction.followup.send(reply)

        @self.tree.command(
            name="summarize", description="Summarize this channel onto its wiki page."
        )
        @app_commands.guild_only()
        async def summarize(interaction: discord.Interaction) -> None:
            await run_analysis(interaction, "summarize", "summarize")

        @self.tree.command(name="stats", description="Publish this channel's activity statistics.")
        @app_commands.guild_only()
        async def stats(interaction: discord.Interaction) -> None:
            await run_analysis(interaction, "stats", "stats")

        @self.tree.command(
            name="talkingpoints", description="Extract this channel's talking points."
        )
        @app_commands.guild_only()
        async def talkingpoints(interaction: discord.Interaction) -> None:
            await run_analysis(interaction, "talkingpoints", "talking_points")

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
