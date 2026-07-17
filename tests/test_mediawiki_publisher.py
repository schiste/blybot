"""MetaWikiPublisher tests (R8) against a scripted in-memory MediaWiki API."""

from __future__ import annotations

import logging
from email.parser import BytesParser
from email.policy import default
from typing import Any, cast
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
        self.uploads: list[tuple[str, bytes, str, str, str]] = []
        self.edit_faults: list[str] = []  # error codes to emit before succeeding
        self.upload_faults: list[str] = []  # error codes to emit before succeeding
        self.login_result = "Success"
        self.requests: list[dict[str, str]] = []
        self.request_files: list[dict[str, tuple[str, bytes, str]]] = []
        self.sections: dict[str, list[str]] = {}  # page -> section headings, in order
        self.parse_faults: list[Exception] = []  # raised (as transport errors) on parse
        self.edit_exceptions: list[Exception] = []  # raised (as transport errors) on edit
        self.upload_exceptions: list[Exception] = []  # raised (as transport errors) on upload
        self.csrf_always_anonymous = False  # simulates a broken login session
        self.login_token_missing = False
        self.transcluded_headings: list[str] = []  # parse-only entries with T- indexes
        self.upload_warning_exists = False

    def handler(self, request: httpx.Request) -> httpx.Response:
        params, files = _request_form(request)
        self.requests.append(params)
        self.request_files.append(files)
        if params["action"] == "parse" and self.parse_faults:
            raise self.parse_faults.pop(0)
        if params["action"] == "edit" and self.edit_exceptions:
            raise self.edit_exceptions.pop(0)
        if params["action"] == "upload" and self.upload_exceptions:
            raise self.upload_exceptions.pop(0)
        return httpx.Response(200, json=self._dispatch(params))

    def _dispatch(self, params: dict[str, str]) -> dict[str, Any]:
        handlers = {
            "query": self._on_query,
            "login": self._on_login,
            "parse": self._on_parse,
            "edit": self._on_edit,
            "upload": self._on_upload,
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

    def _on_upload(self, params: dict[str, str]) -> dict[str, Any]:
        if not self.logged_in or params.get("token") != "CSRF123":
            return {"error": {"code": "badtoken"}}
        if self.upload_faults:
            return {"error": {"code": self.upload_faults.pop(0)}}
        if self.upload_warning_exists:
            return {
                "upload": {
                    "result": "Warning",
                    "filename": params["filename"],
                    "warnings": {"exists": "file exists"},
                }
            }
        filename, content, content_type = self.request_files[-1]["file"]
        assert filename == params["filename"]
        self.uploads.append(
            (params["filename"], content, content_type, params["comment"], params["text"])
        )
        return {"upload": {"result": "Success", "filename": params["filename"]}}


def _request_form(
    request: httpx.Request,
) -> tuple[dict[str, str], dict[str, tuple[str, bytes, str]]]:
    content_type = request.headers.get("content-type", "")
    if not content_type.startswith("multipart/form-data"):
        url_params = {key: values[0] for key, values in parse_qs(request.content.decode()).items()}
        return url_params, {}

    prefix = f"Content-Type: {content_type}\r\nMIME-Version: 1.0\r\n\r\n".encode()
    message = BytesParser(policy=default).parsebytes(prefix + request.read())
    params: dict[str, str] = {}
    files: dict[str, tuple[str, bytes, str]] = {}
    for part in message.iter_parts():
        name = cast("str | None", part.get_param("name", header="content-disposition"))
        if name is None:
            continue
        filename = part.get_filename()
        payload = cast("bytes", part.get_payload(decode=True) or b"")
        if filename is None:
            params[name] = payload.decode()
        else:
            files[name] = (filename, payload, part.get_content_type())
    return params, files


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


async def test_failed_edit_logs_the_mediawiki_error_code(
    caplog: pytest.LogCaptureFixture,
) -> None:
    wiki = FakeWiki()
    wiki.edit_faults = ["protectedpage"]
    publisher, _ = make_publisher(wiki)
    with caplog.at_level(logging.INFO, logger="blybot"), pytest.raises(WikiPublishError):
        await publisher.start_discussion("Talk:Log", "h", ": x", "s")
    assert "event=wiki_edit outcome=error code=protectedpage" in caplog.messages
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


async def test_upload_file_posts_multipart_with_auth_params() -> None:
    wiki = FakeWiki()
    publisher, _ = make_publisher(wiki)
    uploaded = await publisher.upload_file(
        "Blybot_Anon_1.png",
        b"png-bytes",
        "image/png",
        "Log entry via Blybot",
        "file page description",
    )

    assert uploaded == "Blybot_Anon_1.png"
    assert wiki.uploads == [
        (
            "Blybot_Anon_1.png",
            b"png-bytes",
            "image/png",
            "Log entry via Blybot",
            "file page description",
        )
    ]
    upload_request = wiki.requests[-1]
    assert upload_request["action"] == "upload"
    assert upload_request["assert"] == "user"
    assert upload_request["maxlag"] == "5"
    assert upload_request["comment"] == "Log entry via Blybot"
    assert upload_request["text"] == "file page description"
    await publisher.aclose()


async def test_upload_file_retries_maxlag_with_backoff() -> None:
    wiki = FakeWiki()
    wiki.upload_faults = ["maxlag"]
    counters = Counters()
    publisher, sleep = make_publisher(wiki, counters)
    await publisher.upload_file("Blybot_Anon_1.jpg", b"jpeg", "image/jpeg", "s", "d")

    assert wiki.uploads == [("Blybot_Anon_1.jpg", b"jpeg", "image/jpeg", "s", "d")]
    assert sleep.calls == [2.0]
    assert counters.snapshot()["uploads_succeeded"] == 1
    await publisher.aclose()


async def test_upload_file_treats_exists_warning_as_success() -> None:
    wiki = FakeWiki()
    wiki.upload_warning_exists = True
    publisher, _ = make_publisher(wiki)
    uploaded = await publisher.upload_file("Blybot_Anon_1.jpg", b"jpeg", "image/jpeg", "s", "d")

    assert uploaded == "Blybot_Anon_1.jpg"
    await publisher.aclose()


async def test_upload_bad_token_refetches_csrf_and_succeeds() -> None:
    wiki = FakeWiki()
    publisher, _ = make_publisher(wiki)
    await publisher.upload_file("Blybot_Anon_1.jpg", b"first", "image/jpeg", "s", "d")

    wiki.logged_in = False  # cached token is now stale
    await publisher.upload_file("Blybot_Anon_2.jpg", b"second", "image/jpeg", "s", "d")

    assert wiki.uploads[-1] == ("Blybot_Anon_2.jpg", b"second", "image/jpeg", "s", "d")
    await publisher.aclose()


async def test_upload_assertuserfailed_relogs_and_succeeds() -> None:
    wiki = FakeWiki()
    publisher, _ = make_publisher(wiki)
    await publisher.upload_file("Blybot_Anon_1.jpg", b"first", "image/jpeg", "s", "d")

    wiki.logged_in = False
    wiki.upload_faults = ["assertuserfailed"]
    await publisher.upload_file("Blybot_Anon_2.jpg", b"second", "image/jpeg", "s", "d")

    assert wiki.uploads[-1] == ("Blybot_Anon_2.jpg", b"second", "image/jpeg", "s", "d")
    await publisher.aclose()


async def test_upload_non_retryable_error_fails_fast() -> None:
    wiki = FakeWiki()
    wiki.upload_faults = ["protectedpage"]
    publisher, sleep = make_publisher(wiki)

    with pytest.raises(WikiPublishError, match="protectedpage"):
        await publisher.upload_file("Blybot_Anon_1.jpg", b"jpeg", "image/jpeg", "s", "d")

    assert sleep.calls == []
    assert wiki.uploads == []
    await publisher.aclose()


async def test_upload_transport_errors_are_retried_then_fail() -> None:
    wiki = FakeWiki()
    wiki.upload_exceptions = [httpx.ConnectError("down")] * 10
    counters = Counters()
    publisher, _ = make_publisher(wiki, counters)

    with pytest.raises(WikiPublishError, match="http"):
        await publisher.upload_file("Blybot_Anon_1.jpg", b"jpeg", "image/jpeg", "s", "d")

    assert counters.snapshot()["uploads_failed"] == 1
    assert wiki.uploads == []
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
