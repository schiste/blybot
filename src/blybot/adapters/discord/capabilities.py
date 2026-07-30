"""Discord's :class:`~blybot.domain.models.PlatformCapabilities` descriptor.

Kept in its own module (not inlined in ``transport.py`` like Telegram's) so
both the transport and the gateway import the one source of truth for
Discord's per-message cap and feature set without importing each other.

Discord differs from Telegram on exactly the axes the capability gates
exist for: a 2000-character message cap (vs 4096), no deep-link
onboarding and no native chat-picker (onboarding is slash commands), and
snowflake ids that never migrate. Threads, durable DMs, message deletion
and rich choices are all supported.
"""

from __future__ import annotations

from typing import Final

from blybot.domain.models import PlatformCapabilities

DISCORD_CAPABILITIES: Final = PlatformCapabilities(
    max_message_chars=2000,
    threads=True,
    durable_dm=True,
    deep_links=False,
    bot_can_open_dm=True,  # create_dm(): the flow can start in the target channel
    message_delete=True,
    id_can_change=False,
    rich_choices=True,
)
