"""IRC's :class:`~blybot.domain.models.PlatformCapabilities` descriptor.

Kept in its own module (as Discord's is) so services and the composition
root can gate on IRC's shape without importing the transport.
"""

from __future__ import annotations

from typing import Final

from blybot.domain.models import PlatformCapabilities

# RFC 1459 caps a protocol line at 512 bytes *including* the "PRIVMSG
# <target> :" prefix and the trailing CRLF. The prefix varies with channel
# name length, so the transport computes the real budget per message and
# this is the conservative floor features chunk against.
MAX_LINE_BYTES: Final = 512
IRC_CAPABILITIES: Final = PlatformCapabilities(
    max_message_chars=400,
    # No threads, no message deletion, no interactive components: IRC is
    # plain lines to a channel.
    threads=False,
    message_delete=False,
    rich_choices=False,
    deep_links=False,
    # A bot MAY send a PRIVMSG to a nick that never wrote to it, so the
    # destination-picker detour is unnecessary here (#45)...
    bot_can_open_dm=True,
    # ...but a nick is not a durable identity — it can be dropped, taken by
    # someone else, or unregistered — so it must never become the delivery
    # handle a subscription remembers. This is what gates digests off.
    durable_dm=False,
    # Channel names are stable; nothing like a supergroup migration.
    id_can_change=False,
)
