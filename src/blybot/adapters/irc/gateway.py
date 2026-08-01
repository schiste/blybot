"""The IRC gateway: registration, JOINs, and inbound message dispatch.

Two layers, as in the Discord adapter:

* :class:`IrcGateway` is pure — it takes plain values (channel, nick, text)
  and calls the neutral services, returning a reply string or ``None``.
  Every branch is testable without a socket.
* :class:`IrcSession` is the thin shell that owns the connection: it
  registers, joins the configured channels, answers PING, and hands each
  PRIVMSG to the gateway.

Commands arrive as ordinary channel text, so they are addressed to the bot
by prefix (``blybot: help``) or by the ``!`` shorthand — IRC has no
slash-command registry to route them for us.

Authority comes from channel-operator status, tracked live in
:mod:`blybot.adapters.irc.ops` (IRC has no ``get_chat_member`` to ask).
The dispatch table below only maps a verb to a neutral
:class:`~blybot.services.commands.CommandService` call and passes
``is_admin`` — every rule about who may do what, and what the answer says,
stays in the service, shared with Telegram and Discord.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Final

from blybot.adapters.irc.author_mask import IrcAuthorMasker
from blybot.adapters.irc.ops import ChannelOps
from blybot.adapters.irc.protocol import IrcLine, privmsg_lines
from blybot.adapters.irc.scope import scope_of
from blybot.domain.models import CapturedMessage, Scope
from blybot.services.health import log_startup

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Sequence

    from blybot.adapters.irc.connection import LineChannel
    from blybot.domain.ports import Clock
    from blybot.services.capture import CaptureService
    from blybot.services.commands import CommandService
    from blybot.services.directory import ChannelDirectory
    from blybot.services.policy import GroupPolicy

    # One command handler: (gateway, scope, tokens, is_admin) -> reply.
    _Handler = Callable[["IrcGateway", Scope, list[str], bool], Awaitable[str]]

# Commands may be addressed either way; both are conventional on IRC.
_BANG: Final = "!"

# RFC 1459 numerics the session acts on.
_RPL_NAMREPLY: Final = "353"
# Lines that change who holds authority, or who is present to hold it.
_MEMBERSHIP_COMMANDS: Final = frozenset({_RPL_NAMREPLY, "MODE", "KICK", "PART", "QUIT", "NICK"})
_NAMREPLY_MIN_PARAMS: Final = 3  # <me> <symbol> <#channel>
_KICK_MIN_PARAMS: Final = 2  # <#channel> <nick>

REPLY_CAPTURE_USAGE: Final = "Usage: capture on | capture off"
REPLY_HELP: Final = (
    "Commands: capture on|off, setpage <page>, settings, reset, setrepo <owner/repo>, "
    "revoke, llm …, events on|off, rule …, rules, action …, issue <text>, repo. "
    "Address me as 'blybot: <command>' or '!<command>'. Channel operators only, "
    "except issue and repo. No digest subscriptions here: IRC has no durable DM."
)


@dataclass(eq=False)
class IrcGateway:
    """Neutral-service logic behind the IRC session shell."""

    directory: ChannelDirectory
    groups: GroupPolicy
    commands: CommandService
    clock: Clock
    nick: str
    capture: CaptureService | None = None
    masker: IrcAuthorMasker | None = None
    # Live operator tracking — IRC's stand-in for get_chat_member. Holds raw
    # nicks, so it stays here in the adapter and never crosses inward (R6).
    ops: ChannelOps = field(default_factory=ChannelOps)
    # IRC gives no message ids, so one is synthesised per captured line.
    # Monotonic within a process; a restart may repeat values, and the
    # archive's INSERT IGNORE makes that a no-op rather than a duplicate.
    _next_message_id: int = field(default=1, init=False)

    def addressed_command(self, text: str) -> str | None:
        """Return the command text when this line is addressed to the bot.

        ``blybot: help`` and ``!help`` both count; anything else is ordinary
        chatter and must never be parsed as a command.
        """
        stripped = text.strip()
        for lead in (f"{self.nick}:", f"{self.nick},"):
            if stripped.lower().startswith(lead.lower()):
                return stripped[len(lead) :].strip()
        if stripped.startswith(_BANG):
            return stripped[len(_BANG) :].strip()
        return None

    async def run_command(self, channel: str, nick: str, text: str) -> str | None:
        """Route one addressed command and return the reply, or ``None``.

        ``None`` means "not a verb I know" — deliberately silent, because a
        bot that answers every stray ``!foo`` in a busy channel is noise.

        Authority is read here and passed down as a plain bool; no handler
        below decides for itself who counts as an admin, and none of them
        sees the nick at all.
        """
        parts = text.split()
        if not parts:
            return None
        verb, tokens = parts[0].casefold(), parts[1:]
        handler = _DISPATCH.get(verb)
        if handler is None:
            return None
        return await handler(self, scope_of(channel), tokens, self.ops.is_op(channel, nick))

    # --- command handlers: parse the tokens, call the neutral service ------

    async def _cmd_help(self, _scope: Scope, _tokens: list[str], _admin: bool) -> str:
        return REPLY_HELP

    async def _cmd_capture(self, scope: Scope, tokens: list[str], admin: bool) -> str:
        argument = tokens[0].casefold() if tokens else ""
        if argument not in {"on", "off"}:
            return REPLY_CAPTURE_USAGE
        result = await self.commands.capture(scope, is_admin=admin, enabled=argument == "on")
        return result.text

    async def _cmd_setpage(self, scope: Scope, tokens: list[str], admin: bool) -> str:
        result = await self.commands.set_page(scope, is_admin=admin, page=" ".join(tokens))
        return result.text

    async def _cmd_settings(self, scope: Scope, _tokens: list[str], admin: bool) -> str:
        result = await self.commands.show_settings(scope, is_admin=admin)
        return result.text

    async def _cmd_reset(self, scope: Scope, _tokens: list[str], admin: bool) -> str:
        result = await self.commands.reset(scope, is_admin=admin)
        return result.text

    async def _cmd_setrepo(self, scope: Scope, tokens: list[str], admin: bool) -> str:
        result = await self.commands.set_repo(scope, is_admin=admin, repo=" ".join(tokens))
        return result.text

    async def _cmd_settoken(self, scope: Scope, _tokens: list[str], admin: bool) -> str:
        """Refuse a token here — and never look at what was typed.

        The tokens are dropped on the floor rather than forwarded: by the
        time this runs, anything the user typed is already on every client
        in the channel, so the one useful thing left is to not repeat it
        and to tell them to revoke. The refusal itself is the neutral
        service's (``confidential_input``), not this adapter's.
        """
        result = await self.commands.store_token(scope, is_admin=admin, token="")
        return result.text

    async def _cmd_revoke(self, scope: Scope, _tokens: list[str], admin: bool) -> str:
        result = await self.commands.revoke_token(scope, is_admin=admin)
        return result.text

    async def _cmd_llm(self, scope: Scope, tokens: list[str], admin: bool) -> str:
        result = await self.commands.set_llm(scope, is_admin=admin, tokens=tokens)
        return result.text

    async def _cmd_events(self, scope: Scope, tokens: list[str], admin: bool) -> str:
        result = await self.commands.events(scope, is_admin=admin, tokens=tokens)
        return result.text

    async def _cmd_rule(self, scope: Scope, tokens: list[str], admin: bool) -> str:
        result = await self.commands.rule(scope, is_admin=admin, tokens=tokens)
        return result.text

    async def _cmd_rules(self, scope: Scope, _tokens: list[str], admin: bool) -> str:
        result = await self.commands.list_rules(scope, is_admin=admin)
        return result.text

    async def _cmd_action(self, scope: Scope, tokens: list[str], admin: bool) -> str:
        result = await self.commands.action(scope, is_admin=admin, tokens=tokens)
        return result.text

    async def _cmd_issue(self, scope: Scope, tokens: list[str], _admin: bool) -> str:
        # Not admin-gated anywhere: filing is the one repo action any member
        # may take, and the report reaches GitHub with no reporter identity.
        result = await self.commands.file_issue(scope, description=" ".join(tokens))
        return result.text

    async def _cmd_repo(self, scope: Scope, _tokens: list[str], _admin: bool) -> str:
        result = await self.commands.repo_summary(scope)
        return result.text

    async def ingest_message(self, channel: str, nick: str, text: str) -> None:
        """Archive one channel line iff capture is on for it (never raises)."""
        capture, masker = self.capture, self.masker
        if capture is None or masker is None:
            return
        scope = scope_of(channel)
        if not self.groups.is_allowed(scope):
            return
        # Pseudonymize at the boundary, before anything crosses inward (R6).
        author = masker.mask(channel, nick)
        self._next_message_id += 1
        await capture.ingest(
            CapturedMessage(
                scope=scope,
                message_id=self._next_message_id,
                posted_at=self.clock.now(),
                author=author,
                kind="text" if text else "media_note",
                text=text,
                reply_to=None,
            )
        )


# Verb -> handler. A plain table so the surface is auditable at a glance:
# every entry is one neutral service call, and nothing here decides policy.
_DISPATCH: Final[dict[str, _Handler]] = {
    "help": IrcGateway._cmd_help,
    "capture": IrcGateway._cmd_capture,
    "setpage": IrcGateway._cmd_setpage,
    "settings": IrcGateway._cmd_settings,
    "reset": IrcGateway._cmd_reset,
    "setrepo": IrcGateway._cmd_setrepo,
    "settoken": IrcGateway._cmd_settoken,
    "revoke": IrcGateway._cmd_revoke,
    "llm": IrcGateway._cmd_llm,
    "events": IrcGateway._cmd_events,
    "rule": IrcGateway._cmd_rule,
    "rules": IrcGateway._cmd_rules,
    "action": IrcGateway._cmd_action,
    "issue": IrcGateway._cmd_issue,
    "repo": IrcGateway._cmd_repo,
}


@dataclass(eq=False)
class IrcSession:
    """The connection shell: register, join, answer PING, dispatch PRIVMSG."""

    channel: LineChannel
    gateway: IrcGateway
    nick: str
    channels: Sequence[str]
    password: str = ""

    async def register(self) -> None:
        """Send the opening handshake and join the configured channels."""
        if self.password:
            # PASS must precede NICK/USER per RFC 1459 §4.1.1.
            await self.channel.send_line(f"PASS {self.password}")
        await self.channel.send_line(f"NICK {self.nick}")
        await self.channel.send_line(f"USER {self.nick} 0 * :{self.nick}")
        for name in self.channels:
            await self.channel.send_line(f"JOIN {name}")
        log_startup()

    async def handle(self, line: IrcLine) -> None:
        """Dispatch one inbound line."""
        if line.command == "PING":
            # Answering keeps the session alive; missing it gets us killed.
            await self.channel.send_line(f"PONG :{line.trailing}")
            return
        if line.command in _MEMBERSHIP_COMMANDS:
            self._track_membership(line)
            return
        if line.command != "PRIVMSG":
            return
        nick, target = line.nick, line.target
        if not nick or nick.lower() == self.nick.lower():
            return  # unattributable, or our own echo
        if not target.startswith("#"):
            return  # a direct message: no durable_dm, so nothing to do
        command = self.gateway.addressed_command(line.trailing)
        if command is not None:
            reply = await self.gateway.run_command(target, nick, command)
            if reply is not None:
                # Replies go to the CHANNEL, never privately to the caller:
                # on IRC that is not a style choice but the consent model —
                # `capture on` has to be visible to the people being
                # archived, and there is no ephemeral reply to hide it in.
                await self._say(target, reply)
                return
        # Only a RECOGNISED command is withheld from the archive. "blybot:
        # what do you think?" is not a command, it is someone talking in a
        # public channel, and dropping it would leave a hole in the log.
        await self.gateway.ingest_message(target, nick, line.trailing)

    def _track_membership(self, line: IrcLine) -> None:
        """Keep the operator tracker in step with the channel's state."""
        ops = self.gateway.ops
        if line.command == _RPL_NAMREPLY:
            # 353 params: <me> <=|*|@> <#channel>; the names are trailing.
            if len(line.params) >= _NAMREPLY_MIN_PARAMS:
                ops.replace(line.params[2], line.trailing.split())
            return
        if line.command == "MODE":
            if line.params:
                ops.apply_mode(line.params[0], list(line.params[1:]))
            return
        if line.command == "KICK":
            if len(line.params) >= _KICK_MIN_PARAMS:
                ops.revoke(line.params[0], line.params[1])
            return
        # PART, QUIT, and NICK all end the old nick's claim on its status.
        # A freed nick can be taken by someone else, so the grant must not
        # outlive the person who held it.
        if line.command == "PART":
            # Channel-scoped, so a PART naming no channel is simply dropped:
            # widening it to every channel would de-op someone elsewhere on
            # the strength of a malformed line.
            if line.params:
                if line.nick.casefold() == self.nick.casefold():
                    ops.forget_channel(line.params[0])
                else:
                    ops.revoke(line.params[0], line.nick)
            return
        if line.nick:  # QUIT or NICK: the nick itself is gone, everywhere
            ops.forget(line.nick)

    async def _say(self, target: str, text: str) -> None:
        """Send one reply to ``target`` as PRIVMSG lines."""
        for rendered in privmsg_lines(target, text):
            await self.channel.send_line(rendered)

    async def run(self) -> None:
        """Register, then dispatch every inbound line until the peer closes."""
        await self.register()
        async for line in self.channel.lines():
            await self.handle(line)
