"""RepoNotifier tests: rule matching, live + digest delivery, isolation."""

from __future__ import annotations

from blybot.domain.models import EventType, GroupProfile, RepoEvent, Resource, Rule, RuleFilter
from blybot.observability import Counters
from blybot.services.notify import RepoNotifier
from blybot.services.policy import GroupPolicy
from blybot.services.rules import parse_rule
from tests.fakes import FakeRepoGateway, InMemoryProfiles

RELEASE = RepoEvent(event_type=EventType.RELEASE, title="Release 1.0", url="https://x/r/1")
MERGE = RepoEvent(event_type=EventType.PR_MERGED, title="fix", url="https://x/pr/2", author="dev")
ISSUE = RepoEvent(
    event_type=EventType.ISSUE_OPENED, title="bug", url="https://x/i/3", labels=frozenset({"bug"})
)


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


def _rules(*specs: str) -> tuple[Rule, ...]:
    return tuple(parse_rule(spec) for spec in specs)


async def enable(
    store: InMemoryProfiles,
    chat_id: int = -1,
    specs: tuple[str, ...] = ("release digest", "pr.merged digest"),
    token: str | None = "ghp_ok",  # noqa: S107 -- test fixture
    *,
    primed: bool = True,
) -> None:
    await store.upsert(
        GroupProfile(chat_id=chat_id, repo="x/y", events_enabled=True, rules=_rules(*specs))
    )
    if token:
        await store.store_token(chat_id, 0, token)
    if primed:
        # A non-empty cursor per resource takes poll_resource off its
        # baseline so the fake returns its scripted events.
        await store.set_cursors(chat_id, 0, {r.value: "seed" for r in Resource}, "x/y")


async def test_digest_carries_only_matching_events() -> None:
    store, gateway = InMemoryProfiles(), FakeRepoGateway(valid_tokens={"ghp_ok"})
    gateway.events = [RELEASE, MERGE, ISSUE]
    await enable(store)  # release + pr.merged digest rules (issues never polled)
    ((chat_id, _thread, digest),) = await make_notifier(store, gateway).collect()
    assert chat_id == -1
    assert "Release 1.0" in digest
    assert "fix" in digest
    assert "bug" not in digest  # issue.opened has no rule → its stream is not polled


async def test_live_emits_one_message_per_event_prefixed_with_repo() -> None:
    store, gateway = InMemoryProfiles(), FakeRepoGateway(valid_tokens={"ghp_ok"})
    gateway.events = [ISSUE, ISSUE]
    await enable(store, specs=("issue.opened live",))
    messages = await make_notifier(store, gateway).collect()
    assert len(messages) == 2
    assert all(text.startswith("x/y — Issue opened: bug") for _c, _t, text in messages)


async def test_an_event_in_both_modes_is_delivered_live_and_in_digest() -> None:
    store, gateway = InMemoryProfiles(), FakeRepoGateway(valid_tokens={"ghp_ok"})
    gateway.events = [RELEASE]
    await enable(store, specs=("release live", "release digest"))
    messages = [text for _c, _t, text in await make_notifier(store, gateway).collect()]
    assert sum(text.startswith("x/y — ") for text in messages) == 1  # one live line, not two
    assert sum(text.startswith("x/y:") for text in messages) == 1  # one digest


async def test_polled_events_that_match_no_rule_are_silent() -> None:
    store, gateway = InMemoryProfiles(), FakeRepoGateway(valid_tokens={"ghp_ok"})
    gateway.events = [MERGE]  # a PR, but the only rule wants merges with a label
    await enable(store, specs=("pr.merged label:release digest",))
    assert await make_notifier(store, gateway).collect() == []


async def test_first_poll_baselines_without_announcing() -> None:
    store, gateway = InMemoryProfiles(), FakeRepoGateway(valid_tokens={"ghp_ok"})
    gateway.events = [RELEASE]
    await enable(store, specs=("release digest",), primed=False)  # no cursors yet
    assert await make_notifier(store, gateway).collect() == []
    assert store.cursors[-1, 0] == {"releases": "releases|next"}  # baseline written


async def test_scopes_missing_token_repo_or_rules_are_skipped() -> None:
    store, gateway = InMemoryProfiles(), FakeRepoGateway(valid_tokens={"ghp_ok"})
    gateway.events = [RELEASE]
    await enable(store, chat_id=-1, token=None)  # no token
    await store.upsert(
        GroupProfile(chat_id=-2, repo=None, events_enabled=True, rules=_rules("release digest"))
    )
    await store.store_token(-2, 0, "ghp_ok")  # has token but no repo
    await store.upsert(GroupProfile(chat_id=-3, repo="x/y", events_enabled=True))  # no rules
    await store.store_token(-3, 0, "ghp_ok")
    assert await make_notifier(store, gateway).collect() == []


async def test_one_broken_scope_never_blocks_the_others() -> None:
    store, gateway = InMemoryProfiles(), FakeRepoGateway(valid_tokens={"ghp_ok"})
    gateway.events = [RELEASE]
    await enable(store, chat_id=-1, token="ghp_bad")  # noqa: S106 -- rejected by gateway
    await enable(store, chat_id=-2)
    ((chat_id, _thread, _text),) = await make_notifier(store, gateway).collect()
    assert chat_id == -2


async def test_a_bad_stored_regex_in_one_scope_never_blocks_the_others() -> None:
    store, gateway = InMemoryProfiles(), FakeRepoGateway(valid_tokens={"ghp_ok"})
    gateway.events = [RELEASE]
    # A corrupt/hand-edited stored rule with an invalid regex (bypasses the
    # parser). Matching it raises re.error deep inside _deliver.
    bad = Rule(
        rule_id="bad",
        trigger=EventType.RELEASE,
        filter=RuleFilter(title_match="[", title_is_regex=True),
    )
    await store.upsert(GroupProfile(chat_id=-1, repo="x/y", events_enabled=True, rules=(bad,)))
    await store.store_token(-1, 0, "ghp_ok")
    await store.set_cursors(-1, 0, {r.value: "seed" for r in Resource}, "x/y")
    await enable(store, chat_id=-2, specs=("release digest",))  # healthy scope
    ((chat_id, _thread, _text),) = await make_notifier(store, gateway).collect()
    assert chat_id == -2  # the broken scope was isolated, the healthy one delivered


async def test_storage_outage_yields_nothing() -> None:
    store = InMemoryProfiles(fail=True)
    assert await make_notifier(store, FakeRepoGateway()).collect() == []


async def test_fan_out_is_capped() -> None:
    store, gateway = InMemoryProfiles(), FakeRepoGateway(valid_tokens={"ghp_ok"})
    gateway.events = [RELEASE]
    for chat_id in range(-5, 0):
        await enable(store, chat_id=chat_id, specs=("release digest",))
    notifier = make_notifier(store, gateway)
    notifier.max_groups_per_tick = 2
    assert len(await notifier.collect()) == 2  # two scopes processed, one digest each


async def test_digest_truncates_beyond_five_lines() -> None:
    store, gateway = InMemoryProfiles(), FakeRepoGateway(valid_tokens={"ghp_ok"})
    gateway.events = [RELEASE] * 7
    await enable(store, specs=("release digest",))
    ((_c, _t, digest),) = await make_notifier(store, gateway).collect()
    assert digest.count("Release 1.0") == 5
    assert "…and 2 more" in digest


async def test_live_messages_are_capped_with_a_summary() -> None:
    store, gateway = InMemoryProfiles(), FakeRepoGateway(valid_tokens={"ghp_ok"})
    gateway.events = [ISSUE] * 13
    await enable(store, specs=("issue.opened live",))
    messages = await make_notifier(store, gateway).collect()
    assert len(messages) == 11  # 10 individual + one summary
    assert messages[-1][2] == "x/y: …and 3 more live events"


async def test_unchanged_cursor_is_not_rewritten() -> None:
    store, gateway = InMemoryProfiles(), FakeRepoGateway(valid_tokens={"ghp_ok"})
    gateway.events = []
    await enable(store, specs=("release digest",), primed=False)
    await store.set_cursors(-1, 0, {"releases": "releases|next"}, "x/y")  # already at head
    assert await make_notifier(store, gateway).collect() == []
    assert store.cursors[-1, 0] == {"releases": "releases|next"}


async def test_notifications_never_go_to_unlisted_groups() -> None:
    """The operator allowlist gates pushes, not just commands."""
    store, gateway = InMemoryProfiles(), FakeRepoGateway(valid_tokens={"ghp_ok"})
    gateway.events = [RELEASE]
    await enable(store, chat_id=-1)
    assert await make_notifier(store, gateway, allowed={-999}).collect() == []
