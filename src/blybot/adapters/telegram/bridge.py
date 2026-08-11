"""Telegram's inbound edge into the cross-platform bridge (#79).

Deliberately separate from :mod:`blybot.adapters.telegram.capture`, and
registered in its own handler group, because the two read the same update
and then diverge completely:

* capture pseudonymizes the author and archives the text;
* the relay carries the **real** display name sideways to another chat and
  stores nothing.

Neither may see the other's data, and bridging must not require capture to
be on (or vice versa). Reading ``from_user`` is this adapter's job and
stays here — the architecture guard keeps it from crossing inward.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Final

from blybot.adapters.telegram._common import scope_of
from blybot.domain.bridge import RelayMessage

if TYPE_CHECKING:
    from telegram import Message, Update
    from telegram.ext import ContextTypes

    from blybot.services.bridge_router import BridgeRouter

# Attachments relay as a marker, not a re-upload: re-hosting another
# platform's files means storage, expiry, and a copyright question the
# wiki context makes real (see #76).
_MEDIA_MARKERS: Final = (
    ("photo", "[image]"),
    ("sticker", "[sticker]"),
    ("animation", "[gif]"),
    ("video", "[video]"),
    ("voice", "[voice]"),
    ("audio", "[audio]"),
)
# A message with no readable sender still has to be attributed to *something*:
# an unlabelled line in a mirror is worse than an honest placeholder.
_UNKNOWN: Final = "someone"


def describe_media(message: Message) -> str:
    """A short marker for whatever attachment this message carries."""
    document = message.document
    if document is not None:
        return f"[file: {document.file_name}]" if document.file_name else "[file]"
    for attribute, marker in _MEDIA_MARKERS:
        if getattr(message, attribute, None):
            return marker
    return ""


def author_label(message: Message) -> str:
    """The sender's display name, or a stand-in when Telegram gives none."""
    user = message.from_user
    if user is None:
        chat = message.sender_chat
        return (chat.title or _UNKNOWN) if chat is not None else _UNKNOWN
    return user.full_name or user.username or _UNKNOWN


class BridgeHandlers:
    """Relays ordinary group messages into the bridge."""

    def __init__(self, router: BridgeRouter) -> None:
        self.router = router

    async def on_group_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Mirror one group message into every channel bridged to this one."""
        del context
        message = update.effective_message
        if message is None:
            return
        text = message.text or message.caption or describe_media(message)
        if not text:
            return
        await self.router.dispatch(
            RelayMessage(origin=scope_of(update), author=author_label(message), text=text)
        )
