"""The IRC :class:`~blybot.domain.ports.Transport` implementation.

The outbound edge: a neutral :class:`~blybot.domain.models.OutboundMessage`
becomes one or more ``PRIVMSG`` lines, split to fit the protocol's 512-byte
line budget, written to the live connection.

IRC has no reply from the server for a successful PRIVMSG and no rate-limit
response code — servers simply *throttle* by delaying or, if pushed,
disconnecting ("excess flood"). So there is nothing to map to
:class:`RateLimited`; pacing is handled by not flooding in the first place
(the shared delivery loop already spaces sends), and a flood-kill surfaces
as the connection dropping, which is a :class:`TransientTransportError`
the loop retries. This is a real difference from Telegram and Discord,
both of which answer with an explicit retry-after.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from blybot.adapters.irc.capabilities import IRC_CAPABILITIES
from blybot.adapters.irc.protocol import privmsg_lines
from blybot.adapters.irc.scope import irc_target

if TYPE_CHECKING:
    from blybot.adapters.irc.connection import LineChannel
    from blybot.domain.models import OutboundMessage, PlatformCapabilities


@dataclass(eq=False)
class IrcTransport:
    """Sends :class:`OutboundMessage`s as PRIVMSG lines."""

    channel: LineChannel

    @property
    def capabilities(self) -> PlatformCapabilities:
        """IRC's capability descriptor."""
        return IRC_CAPABILITIES

    async def send(self, message: OutboundMessage) -> None:
        """Deliver one message, split across as many lines as it needs."""
        for line in privmsg_lines(irc_target(message.scope), message.text):
            await self.channel.send_line(line)
