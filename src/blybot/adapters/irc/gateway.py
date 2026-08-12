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

from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field, replace
from functools import partial
from typing import TYPE_CHECKING, Final

from blybot.adapters.irc.author_mask import IrcAuthorMasker
from blybot.adapters.irc.ops import ChannelOps
from blybot.adapters.irc.protocol import IrcLine, privmsg_lines
from blybot.adapters.irc.scope import scope_of
from blybot.domain.bridge import RelayMessage
from blybot.domain.models import CapturedMessage, LogContent, Scope
from blybot.domain.ports import PermanentTransportError
from blybot.observability import log_event
from blybot.services.health import log_startup

if TYPE_CHECKING:
    from blybot.adapters.irc.connection import LineChannel
    from blybot.domain.ports import Clock
    from blybot.services.analysis_run import AnalysisService
    from blybot.services.bridge_router import BridgeRouter
    from blybot.services.capture import CaptureService
    from blybot.services.commands import CommandService
    from blybot.services.directory import ChannelDirectory
    from blybot.services.policy import GroupPolicy

    # One command handler: (gateway, scope, tokens, is_admin) -> reply.
    _Handler = Callable[["IrcGateway", "Request"], Awaitable[str]]

# Commands may be addressed either way; both are conventional on IRC.
_BANG: Final = "!"

# How a handler speaks before it has an answer. Defaults to silence so a
# caller with no channel to write to (a test, a dry run) needs no stub.
Say = Callable[[str], Awaitable[None]]


async def _silent(_text: str) -> None:
    """Drop an interim line: no channel is attached to this invocation."""


# RFC 1459 numerics the session acts on.
_RPL_WELCOME: Final = "001"  # registration is complete; JOINs are legal now
_ERR_NICKNAMEINUSE: Final = "433"
_MAX_NICK_ATTEMPTS: Final = 3
_RPL_NAMREPLY: Final = "353"
# Lines that change who holds authority, or who is present to hold it.
_MEMBERSHIP_COMMANDS: Final = frozenset({_RPL_NAMREPLY, "MODE", "KICK", "PART", "QUIT", "NICK"})
_NAMREPLY_MIN_PARAMS: Final = 3  # <me> <symbol> <#channel>
_KICK_MIN_PARAMS: Final = 2  # <#channel> <nick>

REPLY_CAPTURE_USAGE: Final = "Usage: capture on | capture off"
REPLY_ANALYSES_OFF_DEPLOY: Final = "On-demand analyses aren't available on this deployment."
REPLY_ANALYSING: Final = (
    "Analysing… a large window can take a few minutes. I'll post the result here."
)
REPLY_RUN_USAGE: Final = "Usage: run <template> [key=value …]"
REPLY_SUBSCRIBABLE_USAGE: Final = "Usage: subscribable on | off"
REPLY_LOG_USAGE: Final = (
    "Usage: log <text> — I publish that text to this channel's wiki page, unattributed. "
    "IRC gives me no way to point at an earlier message, so quote it yourself."
)
REPLY_HELP: Final = (
    "Commands: capture on|off, setpage <page>, settings, reset, setrepo <owner/repo>, "
    "revoke, llm …, events on|off, rule …, rules, action …, issue <text>, repo. "
    "Address me as 'blybot: <command>' or '!<command>'. Channel operators only, "
    "except issue and repo. No digest subscriptions here: IRC has no durable DM."
)


@dataclass(frozen=True, slots=True)
class Request:
    """One addressed command, with the means to answer mid-run.

    ``say`` exists for the analyses: a chunked run takes minutes, and an
    IRC channel that sees nothing for that long reads the bot as broken.
    Telegram and Discord both acknowledge first; this is how IRC can.
    """

    scope: Scope
    tokens: list[str]
    is_admin: bool
    say: Say


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
    # Set only in the unified runtime, which is the one process that can
    # reach another platform's transport (#76).
    bridge: BridgeRouter | None = None
    # On-demand analyses; absent on a deployment without the archive.
    analysis: AnalysisService | None = None
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

    async def run_command(
        self, channel: str, nick: str, text: str, say: Say = _silent
    ) -> str | None:
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
        return await handler(
            self,
            Request(
                scope=scope_of(channel),
                tokens=tokens,
                is_admin=self.ops.is_op(channel, nick),
                say=say,
            ),
        )

    # --- command handlers: parse the tokens, call the neutral service ------

    async def _cmd_help(self, _request: Request) -> str:
        return REPLY_HELP

    async def _cmd_capture(self, request: Request) -> str:
        argument = request.tokens[0].casefold() if request.tokens else ""
        if argument not in {"on", "off"}:
            return REPLY_CAPTURE_USAGE
        result = await self.commands.capture(
            request.scope, is_admin=request.is_admin, enabled=argument == "on"
        )
        return result.text

    async def _cmd_setpage(self, request: Request) -> str:
        result = await self.commands.set_page(
            request.scope, is_admin=request.is_admin, page=" ".join(request.tokens)
        )
        return result.text

    async def _cmd_settings(self, request: Request) -> str:
        result = await self.commands.show_settings(request.scope, is_admin=request.is_admin)
        return result.text

    async def _cmd_reset(self, request: Request) -> str:
        result = await self.commands.reset(request.scope, is_admin=request.is_admin)
        return result.text

    async def _cmd_setrepo(self, request: Request) -> str:
        result = await self.commands.set_repo(
            request.scope, is_admin=request.is_admin, repo=" ".join(request.tokens)
        )
        return result.text

    async def _cmd_settoken(self, request: Request) -> str:
        """Refuse a token here — and never look at what was typed.

        The tokens are dropped on the floor rather than forwarded: by the
        time this runs, anything the user typed is already on every client
        in the channel, so the one useful thing left is to not repeat it
        and to tell them to revoke. The refusal itself is the neutral
        service's (``confidential_input``), not this adapter's.
        """
        result = await self.commands.store_token(request.scope, is_admin=request.is_admin, token="")
        return result.text

    async def _cmd_revoke(self, request: Request) -> str:
        result = await self.commands.revoke_token(request.scope, is_admin=request.is_admin)
        return result.text

    async def _cmd_llm(self, request: Request) -> str:
        result = await self.commands.set_llm(
            request.scope, is_admin=request.is_admin, tokens=request.tokens
        )
        return result.text

    async def _cmd_events(self, request: Request) -> str:
        result = await self.commands.events(
            request.scope, is_admin=request.is_admin, tokens=request.tokens
        )
        return result.text

    async def _cmd_rule(self, request: Request) -> str:
        result = await self.commands.rule(
            request.scope, is_admin=request.is_admin, tokens=request.tokens
        )
        return result.text

    async def _cmd_rules(self, request: Request) -> str:
        result = await self.commands.list_rules(request.scope, is_admin=request.is_admin)
        return result.text

    async def _cmd_action(self, request: Request) -> str:
        result = await self.commands.action(
            request.scope, is_admin=request.is_admin, tokens=request.tokens
        )
        return result.text

    async def _cmd_issue(self, request: Request) -> str:
        # Not admin-gated anywhere: filing is the one repo action any member
        # may take, and the report reaches GitHub with no reporter identity.
        result = await self.commands.file_issue(request.scope, description=" ".join(request.tokens))
        return result.text

    async def _cmd_bridge(self, request: Request) -> str:
        result = await self.commands.bridge(
            request.scope, is_admin=request.is_admin, tokens=request.tokens
        )
        return result.text

    async def _analysis(self, request: Request, command: str, recipe: str) -> str:
        """Run one on-demand analysis, announcing before the slow part.

        The ack is sent through ``request.say`` rather than returned,
        because a chunked run takes minutes: without it the channel sees
        nothing at all and reads the bot as hung. `on_started` fires only
        once the run is committed to, so a refusal or a bad parse answers
        immediately with no misleading "analysing…".
        """
        service = self.analysis
        if service is None:
            return REPLY_ANALYSES_OFF_DEPLOY
        result = await service.run_analysis(
            request.scope,
            is_admin=request.is_admin,
            command=command,
            recipe=recipe,
            tokens=request.tokens,
            on_started=lambda: request.say(REPLY_ANALYSING),
        )
        return result.text

    async def _cmd_summarize(self, request: Request) -> str:
        return await self._analysis(request, "summarize", "summarize")

    async def _cmd_stats(self, request: Request) -> str:
        return await self._analysis(request, "stats", "stats")

    async def _cmd_talkingpoints(self, request: Request) -> str:
        return await self._analysis(request, "talkingpoints", "talking_points")

    async def _cmd_run(self, request: Request) -> str:
        """`run <template> [key=value …]` — any named prompt template."""
        if not request.tokens:
            return REPLY_RUN_USAGE
        template, rest = request.tokens[0], request.tokens[1:]
        return await self._analysis(replace(request, tokens=rest), "run", f"prompt:{template}")

    async def _cmd_setconsent(self, request: Request) -> str:
        mode = request.tokens[0] if request.tokens else ""
        result = await self.commands.set_consent(
            request.scope, is_admin=request.is_admin, mode=mode
        )
        return result.text

    async def _cmd_subscribable(self, request: Request) -> str:
        """Refused here by `durable_dm`, but offered so the refusal is said."""
        argument = request.tokens[0].casefold() if request.tokens else ""
        if argument not in {"on", "off"}:
            return REPLY_SUBSCRIBABLE_USAGE
        result = await self.commands.set_subscribable(
            request.scope, is_admin=request.is_admin, enabled=argument == "on"
        )
        return result.text

    async def _cmd_log(self, request: Request) -> str:
        """Publish the text the requester typed, unattributed.

        IRC's ``PRIVMSG`` carries no message id and no reply reference, so
        "log *that* message" has nothing to name. Rather than retain recent
        channel lines to make one nameable, this publishes what the
        requester supplies — the bot stores nothing at all.

        The consequence is deliberate and documented: elsewhere ``/log``
        republishes a message the bot *witnessed*; here it republishes an
        assertion about what was said. ``is_author`` is therefore False,
        not because the requester did not write it but because the bot
        cannot know — which fails closed under ``author_only`` consent,
        where the whole point is that only the author may publish.
        """
        text = " ".join(request.tokens).strip()
        if not text:
            return REPLY_LOG_USAGE
        result = await self.commands.log_message(
            request.scope, is_author=False, content=LogContent(text=text)
        )
        return result.text

    async def _cmd_repo(self, request: Request) -> str:
        result = await self.commands.repo_summary(request.scope)
        return result.text

    async def relay_message(self, channel: str, nick: str, text: str) -> None:
        """Mirror one channel line into every channel bridged to this one.

        The nick travels verbatim — a mirror is unreadable without it —
        and never touches the archive, which sees only the HMAC label
        :meth:`ingest_message` computes from the same line.
        """
        router = self.bridge
        if router is None or not text:
            return
        await router.dispatch(RelayMessage(origin=scope_of(channel), author=nick, text=text))

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
    "log": IrcGateway._cmd_log,
    "setconsent": IrcGateway._cmd_setconsent,
    "subscribable": IrcGateway._cmd_subscribable,
    "repo": IrcGateway._cmd_repo,
    "summarize": IrcGateway._cmd_summarize,
    "stats": IrcGateway._cmd_stats,
    "talkingpoints": IrcGateway._cmd_talkingpoints,
    "run": IrcGateway._cmd_run,
    "bridge": IrcGateway._cmd_bridge,
}


@dataclass(eq=False)
class IrcSession:
    """The connection shell: register, join, answer PING, dispatch PRIVMSG."""

    channel: LineChannel
    gateway: IrcGateway
    nick: str
    channels: Sequence[str]
    password: str = ""
    # The realname field, which is where IRC carries "what am I and who
    # runs me". Libera's bot policy requires a bot be clearly labelled as
    # one and attributable to its administrator; this is the only place in
    # the protocol to say it, so it is not cosmetic.
    realname: str = ""
    _registered: bool = field(default=False, init=False)
    _nick_attempts: int = field(default=0, init=False)

    async def register(self) -> None:
        """Send the opening handshake — and nothing else.

        Channels are **not** joined here. A JOIN sent before the server has
        acknowledged registration is dropped or errored by most networks,
        so joining waits for ``001`` (:data:`_RPL_WELCOME`). Sending them
        together happened to work against an in-memory pipe, which is
        exactly the class of bug only a real server reveals.
        """
        handshake = [f"PASS {self.password}"] if self.password else []
        # PASS must precede NICK/USER per RFC 1459 §4.1.1, and the whole
        # handshake is one unit: a relay landing between NICK and USER
        # would be sent before the session is registered.
        handshake += [
            f"NICK {self.nick}",
            f"USER {self.nick} 0 * :{self.realname or self.nick}",
        ]
        await self.channel.send_lines(handshake)

    async def _welcomed(self) -> None:
        """Registration completed: now the channels can be joined."""
        self._registered = True
        if self.channels:
            await self.channel.send_lines([f"JOIN {name}" for name in self.channels])
        log_startup()

    async def _nick_taken(self) -> None:
        """Someone already holds our nick — try a variant, bounded.

        Unhandled, this is the worst kind of failure: the socket stays up
        and PINGs are answered, so the job looks perfectly healthy while
        the bot is never registered and does nothing at all.
        """
        self._nick_attempts += 1
        if self._nick_attempts > _MAX_NICK_ATTEMPTS:
            # Out of variants. Say so loudly: the job runner restarting us
            # is a better outcome than a healthy-looking silent process.
            log_event("irc_register", "error", reason="nick_unavailable")
            msg = f"could not register a nick after {_MAX_NICK_ATTEMPTS} attempts"
            raise PermanentTransportError(msg)
        self.nick = f"{self.nick}_"
        log_event("irc_register", "ignored", reason="nick_in_use")
        await self.channel.send_lines([f"NICK {self.nick}"])

    async def handle(  # noqa: PLR0911 -- one early return per line kind
        self, line: IrcLine
    ) -> None:
        """Dispatch one inbound line."""
        if line.command == "PING":
            # Answering keeps the session alive; missing it gets us killed.
            await self.channel.send_lines((f"PONG :{line.trailing}",))
            return
        if line.command == _RPL_WELCOME:
            await self._welcomed()
            return
        if line.command == _ERR_NICKNAMEINUSE:
            await self._nick_taken()
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
            reply = await self.gateway.run_command(
                target, nick, command, partial(self._say, target)
            )
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
        # The relay forks here too: it carries the nick verbatim sideways
        # and stores nothing, while ingest_message masks and archives.
        await self.gateway.relay_message(target, nick, line.trailing)
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
        """Send one reply to ``target`` as one uninterrupted batch of lines."""
        await self.channel.send_lines(privmsg_lines(target, text))

    async def run(self) -> None:
        """Register, then dispatch every inbound line until the peer closes."""
        await self.register()
        async for line in self.channel.lines():
            await self.handle(line)
