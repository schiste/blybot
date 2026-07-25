"""CaptureService tests: the policy boundary, guards, and cache (v3 §2.7)."""

from __future__ import annotations

from datetime import timedelta

from blybot.domain.models import CapturedMessage, GroupProfile
from blybot.observability import Counters
from blybot.services.capture import MAX_TEXT_CHARS, CaptureService
from blybot.services.policy import SlidingWindowLimiter
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


def msg(message_id: int = 1, text: str = "hello", chat_id: int = -1) -> CapturedMessage:
    return CapturedMessage(
        chat_id=chat_id,
        thread_id=0,
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
