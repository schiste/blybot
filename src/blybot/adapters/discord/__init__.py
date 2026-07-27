"""Discord adapter: gateway (slash commands + capture) and transport.

Exposes the platform's :class:`~blybot.domain.ports.Transport` and its
:class:`~blybot.domain.models.PlatformCapabilities`, the two things the
composition root and the architecture guards look for on any platform
package.
"""

from __future__ import annotations

from blybot.adapters.discord.capabilities import DISCORD_CAPABILITIES
from blybot.adapters.discord.transport import DiscordTransport

__all__ = ["DISCORD_CAPABILITIES", "DiscordTransport"]
