"""RepoNotifier tests: digests, cursors, per-group error isolation."""

from __future__ import annotations

from blybot.domain.models import EventKind, EventType, GroupProfile, RepoEvent
from blybot.observability import Counters
from blybot.services.notify import RepoNotifier
from blybot.services.policy import GroupPolicy
from tests.fakes import FakeRepoGateway, InMemoryProfiles

RELEASE = RepoEvent(event_type=EventType.RELEASE, title="Release 1.0", url="https://x/r/1")
MERGE = RepoEvent(event_type=EventType.PR_MERGED, title="Merged: fix", url="https://x/pr/2")
ISSUE = RepoEvent(event_type=EventType.ISSUE_OPENED, title="New issue: bug", url="https://x/i/3")


def make_notifier(
    store: InMemoryProfiles,
    gateway: FakeRepoGateway,
    allowed: set[int] | None = None,
) -> RepoNotifier:
    return RepoNotifier(
        store=store,
        vault=store,
        gateway=gateway,
        groups=GroupPolicy(allowed=allowed if allowed is not None else set()),
        counters=Counters(),
    )


async def enable(
    store: InMemoryProfiles,
    chat_id: int = -1,
    kinds: frozenset[EventKind] = frozenset({EventKind.RELEASES, EventKind.PRS}),
    token: str | None = "ghp_ok",  # noqa: S107 -- test fixture
) -> None:
    await store.upsert(
        GroupProfile(chat_id=chat_id, repo="x/y", events_enabled=True, event_kinds=kinds)
    )
    if token:
        await store.store_token(chat_id, 0, token)


async def test_digest_carries_only_subscribed_kinds_and_advances_cursor() -> None:
    store, gateway = InMemoryProfiles(), FakeRepoGateway(valid_tokens={"ghp_ok"})
    gateway.events = [RELEASE, MERGE, ISSUE]
    await enable(store)
    await store.set_cursor(-1, 0, "etag|1", "x/y")
    notifier = make_notifier(store, gateway)

    ((chat_id, _thread_id, digest),) = await notifier.collect()
    assert chat_id == -1
    assert "Release 1.0" in digest
    assert "Merged: fix" in digest
    assert "New issue" not in digest  # not subscribed
    assert store.cursors[-1, 0] == "etag|9"


async def test_first_poll_baselines_without_announcing() -> None:
    store, gateway = InMemoryProfiles(), FakeRepoGateway(valid_tokens={"ghp_ok"})
    gateway.events = [RELEASE]
    await enable(store)  # no cursor yet
    notifier = make_notifier(store, gateway)
    assert await notifier.collect() == []
    assert store.cursors[-1, 0] == "etag|1"  # baseline written


async def test_groups_without_token_or_repo_are_skipped() -> None:
    store, gateway = InMemoryProfiles(), FakeRepoGateway(valid_tokens={"ghp_ok"})
    gateway.events = [RELEASE]
    await enable(store, chat_id=-1, token=None)
    await store.upsert(GroupProfile(chat_id=-2, repo=None, events_enabled=True))
    notifier = make_notifier(store, gateway)
    assert await notifier.collect() == []


async def test_one_broken_group_never_blocks_the_others() -> None:
    store = InMemoryProfiles()
    gateway = FakeRepoGateway(valid_tokens={"ghp_ok"})
    gateway.events = [RELEASE]
    await enable(store, chat_id=-1, token="ghp_bad")  # noqa: S106 -- rejected fixture
    await enable(store, chat_id=-2)
    await store.set_cursor(-2, 0, "etag|1", "x/y")
    notifier = make_notifier(store, gateway)

    ((chat_id, _thread, _),) = await notifier.collect()
    assert chat_id == -2


async def test_storage_outage_yields_no_digests() -> None:
    store, gateway = InMemoryProfiles(fail=True), FakeRepoGateway()
    notifier = make_notifier(store, gateway)
    assert await notifier.collect() == []


async def test_fan_out_is_capped_and_logged() -> None:
    store, gateway = InMemoryProfiles(), FakeRepoGateway(valid_tokens={"ghp_ok"})
    gateway.events = [RELEASE]
    for chat_id in range(-5, 0):
        await enable(store, chat_id=chat_id)
        await store.set_cursor(chat_id, 0, "etag|1", "x/y")
    notifier = make_notifier(store, gateway)
    notifier.max_groups_per_tick = 2
    assert len(await notifier.collect()) == 2


async def test_digest_truncates_beyond_five_lines() -> None:
    store, gateway = InMemoryProfiles(), FakeRepoGateway(valid_tokens={"ghp_ok"})
    gateway.events = [RELEASE] * 7
    await enable(store, kinds=frozenset({EventKind.RELEASES}))
    await store.set_cursor(-1, 0, "etag|1", "x/y")
    notifier = make_notifier(store, gateway)
    ((_c, _t, digest),) = await notifier.collect()
    assert digest.count("Release 1.0") == 5
    assert "…and 2 more" in digest


async def test_unchanged_cursor_is_not_rewritten() -> None:
    store, gateway = InMemoryProfiles(), FakeRepoGateway(valid_tokens={"ghp_ok"})
    gateway.events = []
    gateway.next_cursor = "etag|9"
    await enable(store)
    await store.set_cursor(-1, 0, "etag|9", "x/y")  # already at head
    notifier = make_notifier(store, gateway)
    assert await notifier.collect() == []
    assert store.cursors[-1, 0] == "etag|9"


async def test_digests_never_go_to_unlisted_groups() -> None:
    """The operator allowlist gates pushes, not just commands."""
    store, gateway = InMemoryProfiles(), FakeRepoGateway(valid_tokens={"ghp_ok"})
    gateway.events = [RELEASE]
    await enable(store, chat_id=-1)
    await store.set_cursor(-1, 0, "etag|1", "x/y")
    notifier = make_notifier(store, gateway, allowed={-999})
    assert await notifier.collect() == []
