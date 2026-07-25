"""CaptureService tests: the policy boundary, guards, and cache (v3 §2.7)."""

from __future__ import annotations

from datetime import timedelta

from blybot.domain.models import CapturedMessage, GroupProfile
from blybot.observability import Counters
from blybot.services.capture import MAX_TEXT_CHARS, CaptureReminder, CaptureService
from blybot.services.policy import GroupPolicy, SlidingWindowLimiter
from tests.fakes import FakeClock, InMemoryArchive, InMemoryProfiles


def make_service(
    store: InMemoryProfiles,
    archive: InMemoryArchive,
    clock: FakeClock,
    per_minute: int = 100,
) -> tuple[CaptureService, Counters]:
    counters = Counters()
    service = CaptureService(
        store=store,
        archive=archive,
        limiter=SlidingWindowLimiter(clock=clock, limit=per_minute, window=timedelta(minutes=1)),
        clock=clock,
        counters=counters,
    )
    return service, counters


def msg(
    message_id: int = 1, text: str = "hello", chat_id: int = -1, thread_id: int = 0
) -> CapturedMessage:
    return CapturedMessage(
        chat_id=chat_id,
        thread_id=thread_id,
        message_id=message_id,
        posted_at=FakeClock().now(),
        author="abc123",
        text=text,
    )


async def enable(store: InMemoryProfiles, chat_id: int = -1) -> None:
    await store.upsert(GroupProfile(chat_id=chat_id, capture_enabled=True))


async def test_disabled_scope_stores_nothing() -> None:
    store, archive, clock = InMemoryProfiles(), InMemoryArchive(), FakeClock()
    await store.upsert(GroupProfile(chat_id=-1))  # profile exists, capture off
    service, counters = make_service(store, archive, clock)

    await service.ingest(msg())
    await service.ingest(msg(chat_id=-99))  # no profile at all

    assert archive.messages == []
    assert counters.snapshot() == {}


async def test_enabled_scope_is_archived_and_counted() -> None:
    store, archive, clock = InMemoryProfiles(), InMemoryArchive(), FakeClock()
    await enable(store)
    service, counters = make_service(store, archive, clock)

    await service.ingest(msg())

    assert len(archive.messages) == 1
    assert counters.snapshot()["captures"] == 1


async def test_policy_is_cached_until_forgotten_or_expired() -> None:
    store, archive, clock = InMemoryProfiles(), InMemoryArchive(), FakeClock()
    await enable(store)
    service, _counters = make_service(store, archive, clock)
    await service.ingest(msg(1))

    await store.upsert(GroupProfile(chat_id=-1, capture_enabled=False))
    await service.ingest(msg(2))  # cached decision still says on
    assert len(archive.messages) == 2

    service.forget_scope(-1, 0)
    await service.ingest(msg(3))  # cache busted: the off switch applies
    assert len(archive.messages) == 2

    await store.upsert(GroupProfile(chat_id=-1, capture_enabled=True))
    clock.advance(timedelta(seconds=61))  # TTL expiry also re-reads
    await service.ingest(msg(4))
    assert len(archive.messages) == 3


async def test_ingest_ceiling_throttles_a_flood() -> None:
    store, archive, clock = InMemoryProfiles(), InMemoryArchive(), FakeClock()
    await enable(store)
    service, counters = make_service(store, archive, clock, per_minute=2)

    for message_id in range(1, 5):
        await service.ingest(msg(message_id))

    assert len(archive.messages) == 2
    assert counters.snapshot()["captures_throttled"] == 2


async def test_a_topic_opt_out_beats_the_enabled_group_default() -> None:
    store, archive, clock = InMemoryProfiles(), InMemoryArchive(), FakeClock()
    await enable(store)  # group default: on
    await store.upsert(GroupProfile(chat_id=-1, thread_id=7, capture_enabled=False))
    service, _counters = make_service(store, archive, clock)

    await service.ingest(msg(1, thread_id=7))  # explicitly opted out
    await service.ingest(msg(2, thread_id=8))  # undecided: inherits on

    assert [m.thread_id for m in archive.messages] == [8]


async def test_group_capture_off_reaches_inheriting_topics_promptly() -> None:
    store, archive, clock = InMemoryProfiles(), InMemoryArchive(), FakeClock()
    await enable(store)
    service, _counters = make_service(store, archive, clock)
    await service.ingest(msg(1, thread_id=7))  # topic decision now cached

    await store.upsert(GroupProfile(chat_id=-1, capture_enabled=False))
    service.forget_scope(-1, 0)  # what /capture off in General does

    await service.ingest(msg(2, thread_id=7))  # cached True must be gone
    assert [m.thread_id for m in archive.messages] == [7]
    assert archive.messages[0].message_id == 1

    # A topic-level change busts only that topic's entry.
    await store.upsert(GroupProfile(chat_id=-1, thread_id=7, capture_enabled=True))
    service.forget_scope(-1, 7)
    await service.ingest(msg(3, thread_id=7))
    assert [m.message_id for m in archive.messages] == [1, 3]


async def test_denied_scope_never_archives_and_converges_the_disable() -> None:
    store, archive, clock = InMemoryProfiles(), InMemoryArchive(), FakeClock()
    await enable(store)  # durable True that a failed revocation left behind
    service, _counters = make_service(store, archive, clock)

    store.fail = True  # the outage during which consent was revoked
    service.deny_scope(-1, 0)
    await service.ingest(msg(1))  # storage still down: denied, retry fails
    store.fail = False
    await service.ingest(msg(2))  # storage back: still denied, disable lands

    assert archive.messages == []  # nothing archived at any point
    profile = await store.get(-1, 0)
    assert profile is not None
    assert profile.capture_enabled is False  # the retry made it durable
    await service.ingest(msg(3))  # tombstone gone; durable state now rules
    assert archive.messages == []


async def test_retry_denied_converges_quiet_scopes_without_a_message() -> None:
    store, archive, clock = InMemoryProfiles(), InMemoryArchive(), FakeClock()
    await enable(store)  # durable True that a failed revocation left behind
    service, _counters = make_service(store, archive, clock)
    store.fail = True
    service.deny_scope(-1, 0)

    await service.retry_denied()  # storage still down: tombstone stays
    store.fail = False
    await service.retry_denied()  # recovery tick: disable becomes durable

    profile = await store.get(-1, 0)
    assert profile is not None
    assert profile.capture_enabled is False  # no message needed to converge
    await service.ingest(msg(1))
    assert archive.messages == []


async def test_clear_denial_restores_normal_policy_reads() -> None:
    store, archive, clock = InMemoryProfiles(), InMemoryArchive(), FakeClock()
    await enable(store)
    service, _counters = make_service(store, archive, clock)
    service.deny_scope(-1, 0)

    service.clear_denial(-1, 0)  # e.g. a fresh announced enable landed

    await service.ingest(msg(1))
    assert len(archive.messages) == 1


async def test_retry_disable_with_no_stale_row_just_lifts_the_denial() -> None:
    store, archive, clock = InMemoryProfiles(), InMemoryArchive(), FakeClock()
    service, _counters = make_service(store, archive, clock)
    service.deny_scope(-99, 0)  # no profile row exists for this scope

    await service.ingest(msg(1, chat_id=-99))  # nothing to disable: converged
    await service.ingest(msg(2, chat_id=-99))  # denial lifted; policy says off

    assert archive.messages == []


async def test_one_busy_topic_never_throttles_its_siblings() -> None:
    store, archive, clock = InMemoryProfiles(), InMemoryArchive(), FakeClock()
    await enable(store)  # thread 0 covers every topic via inheritance
    service, counters = make_service(store, archive, clock, per_minute=2)

    for message_id in range(1, 5):
        await service.ingest(msg(message_id, thread_id=7))  # capped at 2
    await service.ingest(msg(9, thread_id=8))  # sibling scope: own budget

    assert [m.thread_id for m in archive.messages] == [7, 7, 8]
    assert counters.snapshot()["captures_throttled"] == 2


async def test_oversized_text_is_truncated_at_the_telegram_cap() -> None:
    store, archive, clock = InMemoryProfiles(), InMemoryArchive(), FakeClock()
    await enable(store)
    service, _counters = make_service(store, archive, clock)

    await service.ingest(msg(text="x" * (MAX_TEXT_CHARS + 50)))

    assert len(archive.messages[0].text) == MAX_TEXT_CHARS


async def test_archive_outage_is_swallowed_and_counted() -> None:
    store, archive, clock = InMemoryProfiles(), InMemoryArchive(fail=True), FakeClock()
    await enable(store)
    service, counters = make_service(store, archive, clock)

    await service.ingest(msg())  # must not raise

    assert counters.snapshot() == {"captures_failed": 1}


async def test_profile_outage_fails_closed_without_caching_the_outage() -> None:
    store, archive, clock = InMemoryProfiles(fail=True), InMemoryArchive(), FakeClock()
    service, _counters = make_service(store, archive, clock)

    await service.ingest(msg())
    assert archive.messages == []

    store.fail = False  # storage recovers; the outage was never cached
    await enable(store)
    await service.ingest(msg(2))
    assert len(archive.messages) == 1


async def test_forum_topics_inherit_the_group_capture_default() -> None:
    store, archive, clock = InMemoryProfiles(), InMemoryArchive(), FakeClock()
    await enable(store)  # /capture on in General (thread 0)
    service, _counters = make_service(store, archive, clock)
    topic_message = CapturedMessage(
        chat_id=-1, thread_id=42, message_id=1, posted_at=clock.now(), author="abc", text="hi"
    )

    await service.ingest(topic_message)
    assert len(archive.messages) == 1

    # An explicitly enabled topic works without a group default too.
    await store.upsert(GroupProfile(chat_id=-2, thread_id=7, capture_enabled=True))
    await service.ingest(
        CapturedMessage(
            chat_id=-2, thread_id=7, message_id=2, posted_at=clock.now(), author="x", text="y"
        )
    )
    assert len(archive.messages) == 2


def make_reminder(store: InMemoryProfiles, clock: FakeClock, days: int = 30) -> CaptureReminder:
    return CaptureReminder(
        store=store,
        groups=GroupPolicy(allowed=set()),
        clock=clock,
        cadence=timedelta(days=days),
    )


async def test_reminders_fire_once_per_cadence_never_at_first_sight() -> None:
    store, clock = InMemoryProfiles(), FakeClock()
    await enable(store)
    reminder = make_reminder(store, clock, days=30)

    assert await reminder.collect() == []  # first sighting: baseline only

    clock.advance(timedelta(days=29))
    assert await reminder.collect() == []  # not due yet

    clock.advance(timedelta(days=2))
    (message,) = await reminder.collect()
    assert message.chat_id == -1
    assert "/capture off" in message.text
    assert await reminder.collect() == []  # rescheduled, not repeated


async def test_reminders_respect_the_allowlist_and_storage_outages() -> None:
    store, clock = InMemoryProfiles(), FakeClock()
    await enable(store)
    reminder = make_reminder(store, clock)
    reminder.groups = GroupPolicy(allowed={-999})
    await reminder.collect()
    clock.advance(timedelta(days=31))
    assert await reminder.collect() == []  # disallowed scope stays silent

    store.fail = True
    assert await reminder.collect() == []  # outage: quiet tick
