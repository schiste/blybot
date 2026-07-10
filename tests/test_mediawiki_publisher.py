"""MetaWikiPublisher tests (R8) against a scripted in-memory MediaWiki API."""

from __future__ import annotations

from typing import Any
from urllib.parse import parse_qs

import httpx
import pytest

from blybot.adapters.mediawiki.publisher import MetaWikiPublisher, WikiPublishError
from blybot.observability import Counters

API = "https://wiki.example/w/api.php"


class FakeWiki:
    """Minimal MediaWiki Action API: tokens, login, edit — plus scripted faults."""

    def __init__(self) -> None:
        self.logged_in = False
        self.edits: list[dict[str, str]] = []
        self.edit_faults: list[str] = []  # error codes to emit before succeeding
        self.login_result = "Success"
        self.requests: list[dict[str, str]] = []
        self.sections: dict[str, list[str]] = {}  # page -> section headings, in order
        self.parse_faults: list[Exception] = []  # raised (as transport errors) on parse
        self.edit_exceptions: list[Exception] = []  # raised (as transport errors) on edit
        self.csrf_always_anonymous = False  # simulates a broken login session
        self.login_token_missing = False
        self.transcluded_headings: list[str] = []  # parse-only entries with T- indexes

    def handler(self, request: httpx.Request) -> httpx.Response:
        params = {key: values[0] for key, values in parse_qs(request.content.decode()).items()}
        self.requests.append(params)
        if params["action"] == "parse" and self.parse_faults:
            raise self.parse_faults.pop(0)
        if params["action"] == "edit" and self.edit_exceptions:
            raise self.edit_exceptions.pop(0)
        return httpx.Response(200, json=self._dispatch(params))

    def _dispatch(self, params: dict[str, str]) -> dict[str, Any]:
        handlers = {
            "query": self._on_query,
            "login": self._on_login,
            "parse": self._on_parse,
            "edit": self._on_edit,
        }
        action = params["action"]
        if action not in handlers:
            pytest.fail(f"unexpected action {action}")
        return handlers[action](params)

    def _on_query(self, params: dict[str, str]) -> dict[str, Any]:
        if params.get("type") == "login":
            if self.login_token_missing:
                return {"query": {"tokens": {}}}
            return {"query": {"tokens": {"logintoken": "LOGIN+\\"}}}
        token = "CSRF123" if self.logged_in and not self.csrf_always_anonymous else "+\\"
        return {"query": {"tokens": {"csrftoken": token}}}

    def _on_login(self, params: dict[str, str]) -> dict[str, Any]:
        del params
        self.logged_in = self.login_result == "Success"
        return {"login": {"result": self.login_result}}

    def _on_parse(self, params: dict[str, str]) -> dict[str, Any]:
        page = params["page"]
        if page not in self.sections:
            return {"error": {"code": "missingtitle"}}
        listed: list[dict[str, str]] = [
            {"line": heading, "index": f"T-{position}"}
            for position, heading in enumerate(self.transcluded_headings, start=1)
        ]
        listed += [
            {"line": heading, "index": str(position)}
            for position, heading in enumerate(self.sections[page], start=1)
        ]
        return {"parse": {"sections": listed}}

    def _on_edit(self, params: dict[str, str]) -> dict[str, Any]:
        if not self.logged_in or params.get("token") != "CSRF123":
            return {"error": {"code": "badtoken"}}
        if self.edit_faults:
            return {"error": {"code": self.edit_faults.pop(0)}}
        if params.get("section") == "new":
            self.sections.setdefault(params["title"], []).append(params["sectiontitle"])
        self.edits.append(params)
        return {"edit": {"result": "Success"}}


class SleepRecorder:
    def __init__(self) -> None:
        self.calls: list[float] = []

    async def __call__(self, seconds: float) -> None:
        self.calls.append(seconds)


def make_publisher(
    wiki: FakeWiki, counters: Counters | None = None
) -> tuple[MetaWikiPublisher, SleepRecorder]:
    sleep = SleepRecorder()
    publisher = MetaWikiPublisher(
        api_url=API,
        username="Blybot@blybot",
        botpassword="secret",
        user_agent="Blybot/0.1 (test)",
        counters=counters,
        transport=httpx.MockTransport(wiki.handler),
        sleep=sleep,
    )
    return publisher, sleep


async def test_logs_in_on_demand_and_edits_with_etiquette_params() -> None:
    wiki = FakeWiki()
    publisher, _ = make_publisher(wiki)
    await publisher.start_discussion("Talk:Log", "2026-07-10", ": entry", "Log entry via Blybot")

    (edit,) = wiki.edits
    assert edit["summary"] == "Log entry via Blybot"
    assert edit["assert"] == "user"
    assert edit["maxlag"] == "5"
    await publisher.aclose()


async def test_maxlag_is_retried_with_backoff() -> None:
    wiki = FakeWiki()
    wiki.edit_faults = ["maxlag", "maxlag"]
    counters = Counters()
    publisher, sleep = make_publisher(wiki, counters)
    await publisher.start_discussion("Talk:Log", "h", ": x", "s")

    assert len(wiki.edits) == 1
    assert sleep.calls == [2.0, 4.0]
    assert counters.snapshot()["api_retries"] == 2
    assert counters.snapshot()["publishes_succeeded"] == 1
    await publisher.aclose()


async def test_non_retryable_error_fails_fast() -> None:
    wiki = FakeWiki()
    wiki.edit_faults = ["protectedpage"]
    publisher, sleep = make_publisher(wiki)
    with pytest.raises(WikiPublishError, match="protectedpage"):
        await publisher.start_discussion("Talk:Log", "h", ": x", "s")
    assert sleep.calls == []  # no pointless retry against a protected page
    await publisher.aclose()


async def test_session_loss_triggers_relogin_and_succeeds() -> None:
    wiki = FakeWiki()
    publisher, _ = make_publisher(wiki)
    await publisher.start_discussion("Talk:Log", "h", ": first", "s")

    wiki.logged_in = False  # server-side session drop
    wiki.edit_faults = ["assertuserfailed"]
    await publisher.start_discussion("Talk:Log", "h", ": second", "s")
    assert len(wiki.edits) == 2
    await publisher.aclose()


async def test_bounded_attempts_then_error() -> None:
    wiki = FakeWiki()
    wiki.edit_faults = ["maxlag"] * 10
    counters = Counters()
    publisher, _ = make_publisher(wiki, counters)
    with pytest.raises(WikiPublishError, match="maxlag"):
        await publisher.start_discussion("Talk:Log", "h", ": x", "s")
    assert counters.snapshot()["publishes_failed"] == 1
    assert wiki.edits == []
    await publisher.aclose()


async def test_bad_credentials_raise() -> None:
    wiki = FakeWiki()
    wiki.login_result = "Failed"
    publisher, _ = make_publisher(wiki)
    with pytest.raises(WikiPublishError):
        await publisher.start_discussion("Talk:Log", "h", ": x", "s")
    await publisher.aclose()


async def test_start_discussion_opens_a_new_section() -> None:
    wiki = FakeWiki()
    publisher, _ = make_publisher(wiki)
    await publisher.start_discussion("Talk:Log", "2026-07-10", ": entry", "s")

    (edit,) = wiki.edits
    assert edit["section"] == "new"
    assert edit["sectiontitle"] == "2026-07-10"
    assert edit["text"] == ": entry"
    assert wiki.sections["Talk:Log"] == ["2026-07-10"]
    await publisher.aclose()


async def test_continue_discussion_appends_inside_the_named_section() -> None:
    wiki = FakeWiki()
    publisher, _ = make_publisher(wiki)
    await publisher.start_discussion("Talk:D", "Guest-1", ": first", "s")
    await publisher.start_discussion("Talk:D", "Guest-2", ": other", "s")
    await publisher.continue_discussion("Talk:D", "Guest-1", ":: second", "s")

    last = wiki.edits[-1]
    assert last["section"] == "1"  # Guest-1 is the first section
    assert last["appendtext"] == "\n:: second"
    await publisher.aclose()


async def test_continue_discussion_targets_the_latest_duplicate_heading() -> None:
    wiki = FakeWiki()
    publisher, _ = make_publisher(wiki)
    await publisher.start_discussion("Talk:D", "2026-07-10", ": a", "s")
    await publisher.start_discussion("Talk:D", "2026-07-10", ": b", "s")
    await publisher.continue_discussion("Talk:D", "2026-07-10", ":: reply", "s")
    assert wiki.edits[-1]["section"] == "2"
    await publisher.aclose()


async def test_continue_discussion_creates_the_section_when_missing() -> None:
    """First write of a session, or an archived section: fall back to creating."""
    wiki = FakeWiki()
    publisher, _ = make_publisher(wiki)
    await publisher.continue_discussion("Talk:D", "Guest-9", ": hello", "s")

    (edit,) = wiki.edits
    assert edit["section"] == "new"
    assert edit["sectiontitle"] == "Guest-9"
    assert wiki.sections["Talk:D"] == ["Guest-9"]
    await publisher.aclose()


async def test_no_identifier_ever_reaches_the_wiki_request() -> None:
    """R6 spot check: requests carry only page, text, summary and API plumbing."""
    wiki = FakeWiki()
    publisher, _ = make_publisher(wiki)
    await publisher.start_discussion("Talk:Log", "heading", ": text", "summary")
    await publisher.continue_discussion("Talk:Log", "heading", ":: more", "summary")
    allowed = {
        "action", "format", "meta", "type", "lgname", "lgpassword", "lgtoken",
        "title", "appendtext", "summary", "token", "bot", "maxlag", "assert",
        "section", "sectiontitle", "text", "page", "prop",
    }  # fmt: skip
    for request in wiki.requests:
        assert set(request) <= allowed
    await publisher.aclose()


async def test_transient_parse_failure_is_retried_not_mistaken_for_absence() -> None:
    """A network blip on the section lookup must not fork the discussion."""
    wiki = FakeWiki()
    publisher, sleep = make_publisher(wiki)
    await publisher.start_discussion("Talk:D", "Guest-1", ": first", "s")

    wiki.parse_faults = [httpx.ConnectError("blip")]
    await publisher.continue_discussion("Talk:D", "Guest-1", ":: second", "s")

    assert wiki.sections["Talk:D"] == ["Guest-1"]  # no duplicate section
    assert wiki.edits[-1]["appendtext"] == "\n:: second"
    assert sleep.calls == [2.0]
    await publisher.aclose()


async def test_persistent_parse_failure_raises_instead_of_duplicating() -> None:
    wiki = FakeWiki()
    publisher, _ = make_publisher(wiki)
    await publisher.start_discussion("Talk:D", "Guest-1", ": first", "s")

    wiki.parse_faults = [httpx.ConnectError("down")] * 10
    with pytest.raises(WikiPublishError, match="lookup"):
        await publisher.continue_discussion("Talk:D", "Guest-1", ":: second", "s")
    assert wiki.sections["Talk:D"] == ["Guest-1"]  # still no duplicate
    await publisher.aclose()


async def test_section_archived_between_lookup_and_edit_is_recreated() -> None:
    wiki = FakeWiki()
    publisher, _ = make_publisher(wiki)
    await publisher.start_discussion("Talk:D", "Guest-1", ": first", "s")

    wiki.edit_faults = ["nosuchsection"]  # archived after the lookup
    await publisher.continue_discussion("Talk:D", "Guest-1", ":: second", "s")

    assert wiki.sections["Talk:D"] == ["Guest-1", "Guest-1"]  # recreated
    assert wiki.edits[-1]["section"] == "new"
    await publisher.aclose()


async def test_non_archival_failure_on_continuation_is_not_papered_over() -> None:
    """Only nosuchsection triggers recreation; real failures must surface."""
    wiki = FakeWiki()
    publisher, _ = make_publisher(wiki)
    await publisher.start_discussion("Talk:D", "Guest-1", ": first", "s")

    wiki.edit_faults = ["protectedpage"]
    with pytest.raises(WikiPublishError, match="protectedpage"):
        await publisher.continue_discussion("Talk:D", "Guest-1", ":: second", "s")
    assert wiki.sections["Talk:D"] == ["Guest-1"]  # no bogus recreation
    await publisher.aclose()


async def test_transient_transport_error_on_edit_is_retried() -> None:
    wiki = FakeWiki()
    publisher, sleep = make_publisher(wiki)
    wiki.edit_exceptions = [httpx.ConnectError("blip")]
    await publisher.start_discussion("Talk:D", "Guest-1", ": first", "s")

    assert wiki.sections["Talk:D"] == ["Guest-1"]
    assert sleep.calls == [2.0]
    await publisher.aclose()


async def test_unobtainable_csrf_token_raises_a_credentials_hint() -> None:
    wiki = FakeWiki()
    wiki.csrf_always_anonymous = True  # login "succeeds" but session never sticks
    publisher, _ = make_publisher(wiki)
    with pytest.raises(WikiPublishError, match="CSRF"):
        await publisher.start_discussion("Talk:D", "h", ": x", "s")
    await publisher.aclose()


async def test_missing_login_token_raises() -> None:
    wiki = FakeWiki()
    wiki.login_token_missing = True
    publisher, _ = make_publisher(wiki)
    with pytest.raises(WikiPublishError, match="login token"):
        await publisher.start_discussion("Talk:D", "h", ": x", "s")
    await publisher.aclose()


async def test_transcluded_sections_are_never_edit_targets() -> None:
    """Transcluded sections (index T-n) cannot be edited via section=n."""
    wiki = FakeWiki()
    wiki.transcluded_headings = ["Guest-1"]  # same heading, transcluded from elsewhere
    publisher, _ = make_publisher(wiki)
    await publisher.start_discussion("Talk:D", "Guest-1", ": first", "s")
    await publisher.continue_discussion("Talk:D", "Guest-1", ":: second", "s")

    assert wiki.edits[-1]["section"] == "1"  # the real section, not the transclusion
    await publisher.aclose()
