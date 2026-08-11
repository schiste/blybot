"""Telegram's inbound edge into the bridge (#79)."""

from __future__ import annotations

from typing import TYPE_CHECKING

from telegram import Chat, Document, PhotoSize, Update, User

from blybot.adapters.telegram.bridge import BridgeHandlers, author_label, describe_media
from tests import tg

if TYPE_CHECKING:
    from blybot.domain.bridge import RelayMessage


class _RecordingRouter:
    def __init__(self) -> None:
        self.dispatched: list[RelayMessage] = []

    async def dispatch(self, message: RelayMessage) -> None:
        self.dispatched.append(message)


def _handlers() -> tuple[BridgeHandlers, _RecordingRouter]:
    router = _RecordingRouter()
    return BridgeHandlers(router=router), router  # type: ignore[arg-type]


async def test_a_group_message_is_relayed_with_its_senders_display_name() -> None:
    handlers, router = _handlers()
    context, _bot = tg.make_context()

    await handlers.on_group_message(tg.command_update(tg.message(text="hello")), context)

    (relayed,) = router.dispatched
    assert relayed.author == "Alice"  # the real name, not a pseudonym
    assert relayed.text == "hello"
    assert relayed.origin.platform == "telegram"


async def test_an_attachment_relays_as_a_marker_not_a_re_upload() -> None:
    """Re-hosting another platform's files means storage, expiry and a
    copyright question the wiki context makes real (#76)."""
    handlers, router = _handlers()
    context, _bot = tg.make_context()
    photo = tg.message(text=None, photo=(PhotoSize("f", "u", 1, 1),))

    await handlers.on_group_message(tg.command_update(photo), context)

    assert router.dispatched[0].text == "[image]"


def test_a_named_document_keeps_its_filename() -> None:
    named = tg.message(text=None, document=Document("f", "u", file_name="notes.pdf"))
    assert describe_media(named) == "[file: notes.pdf]"
    assert describe_media(tg.message(text=None, document=Document("f", "u"))) == "[file]"
    assert describe_media(tg.message(text="just text")) == ""


async def test_a_caption_is_relayed_in_preference_to_the_marker() -> None:
    handlers, router = _handlers()
    context, _bot = tg.make_context()
    captioned = tg.message(text=None, photo=(PhotoSize("f", "u", 1, 1),), caption="on the left")

    await handlers.on_group_message(tg.command_update(captioned), context)

    assert router.dispatched[0].text == "on the left"


async def test_a_message_with_nothing_to_mirror_is_skipped() -> None:
    handlers, router = _handlers()
    context, _bot = tg.make_context()

    await handlers.on_group_message(tg.command_update(tg.message(text=None)), context)
    await handlers.on_group_message(Update(update_id=1), context)  # no message at all

    assert router.dispatched == []


def test_an_unreadable_sender_still_gets_an_honest_placeholder() -> None:
    """An unattributed line in a mirror is worse than a named placeholder."""
    channel = Chat(id=-100, type=Chat.CHANNEL, title="Announcements")
    assert author_label(tg.message(text="hi", from_user=None, sender_chat=channel)) == (
        "Announcements"
    )
    assert author_label(tg.message(text="hi", from_user=None)) == "someone"
    nameless = User(id=9, first_name="", is_bot=False, username="handle")
    assert author_label(tg.message(text="hi", from_user=nameless)) == "handle"
