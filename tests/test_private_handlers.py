"""DM session and newcomer welcome handler tests (spec R4, R5)."""

from __future__ import annotations

from datetime import timedelta
from types import SimpleNamespace

from telegram import Update, User
from telegram.constants import ChatMemberStatus
from telegram.error import TelegramError

from blybot.adapters.telegram import handlers as h
from blybot.domain.models import ConsentMode
from blybot.domain.ports import IssueTrackerError
from blybot.observability import Counters
from blybot.services.binding import TokenBinding
from blybot.services.directory import ChannelDirectory
from blybot.services.feedback import FeedbackService
from blybot.services.policy import SlidingWindowLimiter
from tests import tg
from tests.fakes import (
    FailingPublisher,
    FakeClock,
    FakePublisher,
    FakeRepoGateway,
    InMemoryProfiles,
)
from tests.test_group_handlers import make_handlers as make_group_handlers
from tests.test_transcribe import make_service

TTL = timedelta(minutes=45)


ISSUES_URL = "https://github.com/schiste/blybot/issues"


class FakeTracker:
    def __init__(self) -> None:
        self.issues: list[tuple[str, str]] = []
        self.fail = False

    async def open_issue(self, title: str, body: str) -> str:
        if self.fail:
            raise IssueTrackerError
        self.issues.append((title, body))
        return f"{ISSUES_URL}/42"


def make_handlers(
    clock: FakeClock | None = None,
    tracker: FakeTracker | None = None,
    bug_limit: int = 100,
    store: InMemoryProfiles | None = None,
    gateway: FakeRepoGateway | None = None,
) -> tuple[h.PrivateHandlers, FakePublisher]:
    clock = clock or FakeClock()
    publisher = FakePublisher()
    transcription = make_service(publisher, clock)
    store = store if store is not None else InMemoryProfiles()
    handlers = h.PrivateHandlers(
        transcription=transcription,
        sessions=transcription.sessions,
        counters=Counters(),
        welcome_text="Welcome to Blybot.",
        dm_page_url="https://meta.wikimedia.org/wiki/Meta_talk:Community/Discussions",
        maintainer="Test Maintainer",
        issues_url=ISSUES_URL,
        feedback=FeedbackService(tracker) if tracker else None,
        bug_limiter=SlidingWindowLimiter(clock=clock, limit=bug_limit, window=timedelta(hours=1)),
        binding=TokenBinding(clock=clock),
        directory=ChannelDirectory(
            store=store,
            default_log_page="Next 25/Telegram logs",
            default_consent=ConsentMode.IMMEDIATE,
            default_repo="",
            page_prefix="Telegram logs/",
        ),
        gateway=gateway if gateway is not None else FakeRepoGateway(),
        vault=store,
    )
    return handlers, publisher


def dm(text: str | None) -> Update:
    return tg.command_update(tg.message(chat=tg.PRIVATE, text=text, from_user=tg.ALICE))


async def test_start_delivers_the_welcome_and_nothing_else() -> None:
    """/start is the doorway (R5): welcome copy only — no session side effects."""
    handlers, _ = make_handlers()
    context, bot = tg.make_context()
    await handlers.on_start(dm("/start welcome"), context)

    assert tg.sent_texts(bot) == ["Welcome to Blybot."]
    assert handlers.sessions.peek(tg.PRIVATE.id) is None  # no identity minted


async def test_flush_forces_a_fresh_identity_and_announces_it() -> None:
    handlers, _ = make_handlers()
    context, bot = tg.make_context()
    await handlers.on_dm(dm("hello"), context)  # session Anon-1 opens lazily
    await handlers.on_flush(dm("/flush"), context)

    session = handlers.sessions.peek(tg.PRIVATE.id)
    assert session is not None
    assert session.pseudonym.value == "Anon-2"
    assert "Anon-2" in tg.sent_texts(bot)[-1]
    assert h.REPLY_FLUSHED.strip() in tg.sent_texts(bot)[-1]


async def test_whoami_discloses_without_rotating() -> None:
    handlers, _ = make_handlers()
    context, bot = tg.make_context()
    await handlers.on_dm(dm("hello"), context)
    await handlers.on_whoami(dm("/whoami"), context)

    assert "Anon-1" in tg.sent_texts(bot)[-1]
    session = handlers.sessions.peek(tg.PRIVATE.id)
    assert session is not None
    assert session.pseudonym.value == "Anon-1"  # unchanged


async def test_whoami_without_a_session_explains_lazy_minting() -> None:
    handlers, _ = make_handlers()
    context, bot = tg.make_context()
    await handlers.on_whoami(dm("/whoami"), context)
    assert tg.sent_texts(bot) == [h.REPLY_NO_SESSION]


async def test_private_help_lists_the_commands() -> None:
    handlers, _ = make_handlers()
    context, bot = tg.make_context()
    await handlers.on_help(dm("/help"), context)
    (sent,) = tg.sent_texts(bot)
    for command in ("/whoami", "/flush", "/privacy", "/log"):
        assert command in sent
    assert "https://meta.wikimedia.org/wiki/Meta_talk:Community/Discussions" in sent
    assert "maintained by Test Maintainer" in sent


async def test_privacy_statement_covers_the_guarantees() -> None:
    handlers, _ = make_handlers()
    context, bot = tg.make_context()
    await handlers.on_privacy(dm("/privacy"), context)
    (sent,) = tg.sent_texts(bot)
    assert "pseudonym" in sent
    assert "permanently" in sent
    assert "Toolforge" in sent
    assert "https://github.com/schiste/blybot" in sent
    assert "AGPL" in sent


async def test_private_commands_outside_private_chats_are_ignored() -> None:
    handlers, _ = make_handlers()
    context, bot = tg.make_context()
    group_update = tg.command_update(tg.message(chat=tg.GROUP, text="/x"))
    for handler in (
        handlers.on_start,
        handlers.on_flush,
        handlers.on_whoami,
        handlers.on_help,
        handlers.on_privacy,
    ):
        await handler(group_update, context)
    assert tg.sent_texts(bot) == []


async def test_dm_is_transcribed_under_the_session_pseudonym() -> None:
    handlers, publisher = make_handlers()
    context, _ = tg.make_context()
    await handlers.on_dm(dm("hello there"), context)

    (page, heading, text, _) = publisher.started[0]
    assert page == "Meta talk:Community/Discussions"
    assert heading == "Anon-1"
    assert text == ": [sanitized]hello there --Anon-1"


async def test_first_dm_announces_the_identity_then_stays_quiet() -> None:
    handlers, _ = make_handlers()
    context, bot = tg.make_context()
    await handlers.on_dm(dm("first"), context)
    await handlers.on_dm(dm("second"), context)

    (announcement,) = tg.sent_texts(bot)
    assert "Anon-1" in announcement


async def test_session_rollover_mid_conversation_is_announced() -> None:
    clock = FakeClock()
    handlers, _ = make_handlers(clock)
    context, bot = tg.make_context()
    await handlers.on_dm(dm("before"), context)
    clock.advance(TTL)
    await handlers.on_dm(dm("after"), context)

    texts = tg.sent_texts(bot)
    assert len(texts) == 2
    assert "Anon-2" in texts[1]


async def test_group_messages_never_reach_transcription() -> None:
    handlers, publisher = make_handlers()
    context, _ = tg.make_context()
    group_msg = tg.command_update(tg.message(chat=tg.GROUP, text="group chatter"))
    await handlers.on_dm(group_msg, context)
    assert publisher.wrote_nothing


async def test_newcomer_gets_a_deep_link_button_not_a_dm() -> None:
    group_handlers, _, _ = make_group_handlers()
    context, bot = tg.make_context()
    join = tg.membership_update(tg.GROUP, user=tg.ALICE, joined=True, mine=False)
    await group_handlers.on_newcomer(join, context)

    call = bot.send_message.await_args
    assert call is not None
    assert call.kwargs["chat_id"] == tg.GROUP.id  # posted in the group, never a DM
    assert call.kwargs["text"] == h.NEWCOMER_PROMPT
    button = call.kwargs["reply_markup"].inline_keyboard[0][0]
    assert button.url == "https://t.me/blybot_bot?start=welcome"


async def test_joining_bots_are_not_welcomed() -> None:
    group_handlers, _, _ = make_group_handlers()
    context, bot = tg.make_context()
    robot = User(id=99, first_name="OtherBot", is_bot=True)
    join = tg.membership_update(tg.GROUP, user=robot, joined=True, mine=False)
    await group_handlers.on_newcomer(join, context)
    assert tg.sent_texts(bot) == []


async def test_newcomers_in_unlisted_groups_are_ignored() -> None:
    group_handlers, _, _ = make_group_handlers(allowed={-42})
    context, bot = tg.make_context()
    join = tg.membership_update(tg.GROUP, user=tg.ALICE, joined=True, mine=False)
    await group_handlers.on_newcomer(join, context)
    assert tg.sent_texts(bot) == []


async def test_dm_without_text_is_ignored() -> None:
    handlers, publisher = make_handlers()
    context, bot = tg.make_context()
    await handlers.on_dm(dm(None), context)
    assert publisher.wrote_nothing
    assert tg.sent_texts(bot) == []


async def test_dm_wiki_failure_reports_neutrally_and_skips_the_announcement() -> None:
    clock = FakeClock()
    transcription = make_service(FailingPublisher(), clock)
    store = InMemoryProfiles()
    handlers = h.PrivateHandlers(
        transcription=transcription,
        sessions=transcription.sessions,
        counters=Counters(),
        welcome_text="Welcome.",
        dm_page_url="https://example.org/wiki/D",
        maintainer="",
        issues_url=ISSUES_URL,
        feedback=None,
        bug_limiter=SlidingWindowLimiter(clock=clock, limit=100, window=timedelta(hours=1)),
        binding=TokenBinding(clock=clock),
        directory=ChannelDirectory(
            store=store,
            default_log_page="P",
            default_consent=ConsentMode.IMMEDIATE,
            default_repo="",
            page_prefix="",
        ),
        gateway=FakeRepoGateway(),
        vault=store,
    )
    context, bot = tg.make_context()
    await handlers.on_dm(dm("doomed"), context)
    assert tg.sent_texts(bot) == [h.REPLY_WIKI_ERROR]


def test_help_footer_omits_the_maintainer_line_when_unset() -> None:
    footer = h._help_footer("https://example.org/wiki/P", "")
    assert "lands at https://example.org/wiki/P" in footer
    assert "maintained" not in footer


async def test_bug_files_an_anonymous_issue_and_links_it() -> None:
    tracker = FakeTracker()
    handlers, _ = make_handlers(tracker=tracker)
    context, bot = tg.make_context(args=["the", "bot", "broke"])
    await handlers.on_bug(dm("/bug the bot broke"), context)

    ((title, body),) = tracker.issues
    assert title == "the bot broke"
    assert "anonymously" in body
    assert tg.sent_texts(bot) == [h.REPLY_BUG_FILED.format(url=f"{ISSUES_URL}/42")]


async def test_bug_without_description_shows_usage() -> None:
    handlers, _ = make_handlers(tracker=FakeTracker())
    context, bot = tg.make_context()
    await handlers.on_bug(dm("/bug"), context)
    assert tg.sent_texts(bot) == [h.REPLY_BUG_USAGE]


async def test_bug_when_unconfigured_points_at_the_tracker() -> None:
    handlers, _ = make_handlers(tracker=None)
    context, bot = tg.make_context(args=["broken"])
    await handlers.on_bug(dm("/bug broken"), context)
    assert tg.sent_texts(bot) == [h.REPLY_BUG_DISABLED.format(url=ISSUES_URL)]


async def test_bug_reports_are_rate_limited() -> None:
    tracker = FakeTracker()
    handlers, _ = make_handlers(tracker=tracker, bug_limit=1)
    context, bot = tg.make_context(args=["x"])
    await handlers.on_bug(dm("/bug x"), context)
    await handlers.on_bug(dm("/bug x"), context)
    assert len(tracker.issues) == 1
    assert tg.sent_texts(bot)[-1] == h.REPLY_THROTTLED


async def test_bug_filing_failure_falls_back_to_the_tracker_url() -> None:
    tracker = FakeTracker()
    tracker.fail = True
    handlers, _ = make_handlers(tracker=tracker)
    context, bot = tg.make_context(args=["x"])
    await handlers.on_bug(dm("/bug x"), context)
    assert tg.sent_texts(bot) == [h.REPLY_BUG_FAILED.format(url=ISSUES_URL)]


async def test_bug_outside_private_chat_is_ignored() -> None:
    handlers, _ = make_handlers(tracker=FakeTracker())
    context, bot = tg.make_context(args=["x"])
    await handlers.on_bug(tg.command_update(tg.message(chat=tg.GROUP, text="/bug x")), context)
    assert tg.sent_texts(bot) == []


async def test_newcomer_prompt_can_be_switched_off() -> None:
    group_handlers, _, _ = make_group_handlers(newcomer_welcome_enabled=False)
    context, bot = tg.make_context()
    join = tg.membership_update(tg.GROUP, user=tg.ALICE, joined=True, mine=False)
    await group_handlers.on_newcomer(join, context)
    assert tg.sent_texts(bot) == []


async def test_config_deep_link_arms_token_entry_for_admins() -> None:
    store = InMemoryProfiles()
    handlers, _ = make_handlers(store=store)
    await handlers.directory.set_repo(tg.GROUP.id, "wikimedia/mediawiki")
    nonce = handlers.binding.mint_link(tg.GROUP.id)
    context, bot = tg.make_context(args=[f"cfg_{nonce}"])
    bot.get_chat_member.return_value = SimpleNamespace(status=ChatMemberStatus.ADMINISTRATOR)

    await handlers.on_start(dm(f"/start cfg_{nonce}"), context)

    (sent,) = tg.sent_texts(bot)
    assert "wikimedia/mediawiki" in sent
    assert "delete your message from this chat immediately" in sent
    assert handlers.binding.pending_group(tg.PRIVATE.id) == tg.GROUP.id


async def test_expired_or_bogus_links_are_refused() -> None:
    handlers, _ = make_handlers()
    context, bot = tg.make_context(args=["cfg_nope"])
    await handlers.on_start(dm("/start cfg_nope"), context)
    assert tg.sent_texts(bot) == [h.REPLY_LINK_EXPIRED]


async def test_non_admins_cannot_redeem_a_link() -> None:
    handlers, _ = make_handlers()
    await handlers.directory.set_repo(tg.GROUP.id, "x/y")
    nonce = handlers.binding.mint_link(tg.GROUP.id)
    context, bot = tg.make_context(args=[f"cfg_{nonce}"])
    bot.get_chat_member.return_value = SimpleNamespace(status=ChatMemberStatus.MEMBER)
    await handlers.on_start(dm("/start"), context)
    assert tg.sent_texts(bot) == [h.REPLY_LINK_NOT_ADMIN]
    assert handlers.binding.pending_group(tg.PRIVATE.id) is None
    # Griefing guard: the non-admin tap did NOT burn the admin's link.
    assert handlers.binding.peek_link(nonce) == tg.GROUP.id


async def test_link_without_a_bound_repo_instructs_setrepo() -> None:
    handlers, _ = make_handlers()
    nonce = handlers.binding.mint_link(tg.GROUP.id)
    context, bot = tg.make_context(args=[f"cfg_{nonce}"])
    bot.get_chat_member.return_value = SimpleNamespace(status=ChatMemberStatus.ADMINISTRATOR)
    await handlers.on_start(dm("/start"), context)
    assert tg.sent_texts(bot) == [h.REPLY_PAT_NO_REPO]


async def test_pasted_token_is_stored_encrypted_and_never_transcribed() -> None:
    """THE privacy property of this flow: an armed entry swallows the
    message before transcription can publish it."""
    store = InMemoryProfiles()
    gateway = FakeRepoGateway(valid_tokens={"ghp_good"})
    handlers, publisher = make_handlers(store=store, gateway=gateway)
    await handlers.directory.set_repo(tg.GROUP.id, "x/y")
    handlers.binding.open_entry(tg.PRIVATE.id, tg.GROUP.id)
    context, bot = tg.make_context()

    await handlers.on_dm(dm("ghp_good"), context)

    assert publisher.wrote_nothing  # the token never reached the wiki
    assert store.tokens[tg.GROUP.id] == "ghp_good"
    assert handlers.binding.pending_group(tg.PRIVATE.id) is None
    assert tg.sent_texts(bot) == [h.REPLY_PAT_SAVED]
    # The bot removed the pasted secret from the chat itself.
    delete = bot.delete_message.await_args
    assert delete is not None
    assert delete.kwargs["chat_id"] == tg.PRIVATE.id


async def test_rejected_token_can_be_retried() -> None:
    store = InMemoryProfiles()
    gateway = FakeRepoGateway(valid_tokens={"ghp_good"})
    handlers, publisher = make_handlers(store=store, gateway=gateway)
    await handlers.directory.set_repo(tg.GROUP.id, "x/y")
    handlers.binding.open_entry(tg.PRIVATE.id, tg.GROUP.id)
    context, bot = tg.make_context()

    await handlers.on_dm(dm("ghp_wrong"), context)
    assert tg.sent_texts(bot) == [h.REPLY_PAT_INVALID]
    assert handlers.binding.pending_group(tg.PRIVATE.id) == tg.GROUP.id  # still armed

    await handlers.on_dm(dm("ghp_good"), context)
    assert store.tokens[tg.GROUP.id] == "ghp_good"
    assert publisher.wrote_nothing


async def test_token_entry_with_repo_reset_midway_aborts() -> None:
    handlers, publisher = make_handlers()
    handlers.binding.open_entry(tg.PRIVATE.id, tg.GROUP.id)  # repo never bound
    context, bot = tg.make_context()
    await handlers.on_dm(dm("ghp_x"), context)
    assert tg.sent_texts(bot) == [h.REPLY_PAT_NO_REPO]
    assert handlers.binding.pending_group(tg.PRIVATE.id) is None
    assert publisher.wrote_nothing


async def test_token_store_failure_keeps_the_entry_armed() -> None:
    store = InMemoryProfiles()
    gateway = FakeRepoGateway(valid_tokens={"ghp_good"})
    handlers, _ = make_handlers(store=store, gateway=gateway)
    await handlers.directory.set_repo(tg.GROUP.id, "x/y")
    handlers.binding.open_entry(tg.PRIVATE.id, tg.GROUP.id)
    store.fail_token_writes = True
    context, bot = tg.make_context()
    await handlers.on_dm(dm("ghp_good"), context)
    assert tg.sent_texts(bot) == [h.REPLY_PAT_STORE_FAILED]
    assert handlers.binding.pending_group(tg.PRIVATE.id) == tg.GROUP.id


async def test_token_entry_without_gateway_aborts_defensively() -> None:
    handlers, publisher = make_handlers()
    handlers.gateway = None  # self-service wiring mismatch: fail closed
    handlers.binding.open_entry(tg.PRIVATE.id, tg.GROUP.id)
    context, bot = tg.make_context()
    await handlers.on_dm(dm("ghp_x"), context)
    assert tg.sent_texts(bot) == [h.REPLY_PAT_NO_REPO]
    assert handlers.binding.pending_group(tg.PRIVATE.id) is None
    assert publisher.wrote_nothing


async def test_link_consumed_in_a_race_reads_as_expired() -> None:
    handlers, _ = make_handlers()
    await handlers.directory.set_repo(tg.GROUP.id, "x/y")
    nonce = handlers.binding.mint_link(tg.GROUP.id)
    handlers.binding.redeem_link = lambda _n: None  # type: ignore[method-assign, assignment]
    context, bot = tg.make_context(args=[f"cfg_{nonce}"])
    bot.get_chat_member.return_value = SimpleNamespace(status=ChatMemberStatus.ADMINISTRATOR)
    await handlers.on_start(dm("/start"), context)
    assert tg.sent_texts(bot) == [h.REPLY_LINK_EXPIRED]


async def test_pat_message_deletion_failure_does_not_block_the_flow() -> None:
    store = InMemoryProfiles()
    gateway = FakeRepoGateway(valid_tokens={"ghp_good"})
    handlers, _ = make_handlers(store=store, gateway=gateway)
    await handlers.directory.set_repo(tg.GROUP.id, "x/y")
    handlers.binding.open_entry(tg.PRIVATE.id, tg.GROUP.id)
    context, bot = tg.make_context()
    bot.delete_message.side_effect = TelegramError("gone")
    await handlers.on_dm(dm("ghp_good"), context)
    assert store.tokens[tg.GROUP.id] == "ghp_good"  # storage still succeeded
