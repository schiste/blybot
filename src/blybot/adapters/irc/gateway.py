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

**Command routing is not wired yet** (issue #21). :meth:`addressed_command`
recognizes an addressed line and :attr:`IrcGateway.commands` is injected,
but nothing dispatches to the service: every admin command would otherwise
be callable by any nick in the channel, since IRC carries no permission
model the way Telegram's ``get_chat_member`` or Discord's
``guild_permissions`` do. Establishing the channel-op check is #21's whole
job, and shipping the routing before it would open exactly the hole the
admin gate exists to close. Capture ingestion below needs no such gate: it
is switched on per channel by an operator, not by a chat command.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Final

from blybot.adapters.irc.author_mask import IrcAuthorMasker
from blybot.adapters.irc.protocol import IrcLine
from blybot.adapters.irc.scope import scope_of
from blybot.domain.models import CapturedMessage
from blybot.services.health import log_startup

if TYPE_CHECKING:
    from collections.abc import Sequence

    from blybot.adapters.irc.connection import LineChannel
    from blybot.domain.ports import Clock
    from blybot.services.capture import CaptureService
    from blybot.services.commands import CommandService
    from blybot.services.directory import ChannelDirectory
    from blybot.services.policy import GroupPolicy

# Commands may be addressed either way; both are conventional on IRC.
_BANG: Final = "!"


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
        if line.command != "PRIVMSG":
            return
        nick, target = line.nick, line.target
        if not nick or nick.lower() == self.nick.lower():
            return  # unattributable, or our own echo
        if not target.startswith("#"):
            return  # a direct message: no durable_dm, so nothing to do
        await self.gateway.ingest_message(target, nick, line.trailing)

    async def run(self) -> None:
        """Register, then dispatch every inbound line until the peer closes."""
        await self.register()
        async for line in self.channel.lines():
            await self.handle(line)
