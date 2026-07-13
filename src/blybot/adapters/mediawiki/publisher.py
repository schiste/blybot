"""Meta-wiki publisher (spec sections 8-9, R8).

Implements :class:`blybot.domain.ports.WikiPublisher` directly against the
MediaWiki Action API with ``httpx`` (async, and already in the dependency
tree via python-telegram-bot):

* ``section=new`` for new discussions and ``appendtext`` into a located
  section for continuations — both server-side appends, conflict-free;
* BotPassword login with automatic re-login on ``assertuserfailed``;
* ``assert=user`` on every edit so a dropped session fails loudly
  instead of editing logged-out;
* ``maxlag=5`` honored with bounded exponential backoff, also applied to
  ``ratelimited``/``readonly`` and transient HTTP failures;
* descriptive ``User-Agent`` from configuration (WMF policy).

Known trade-off: retrying after a *lost response* is not idempotent for
``section=new`` — if the server committed the edit but the response
timed out, the retry creates a duplicate section. The MediaWiki API
offers no cheap idempotency token; this rare duplication is accepted
over silently dropping entries.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any, Final

import httpx

from blybot.domain.ports import WikiWriteError
from blybot.observability import Counters, log_event

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

_MAXLAG_SECONDS: Final = "5"
_RETRYABLE_API_CODES: Final = frozenset({"maxlag", "ratelimited", "readonly", "editconflict"})
# MediaWiki returns this fixed sentinel as the CSRF token for anonymous
# (logged-out) clients. Not a credential.
_ANONYMOUS_TOKEN: Final = "+\\"  # noqa: S105


class WikiPublishError(WikiWriteError):
    """Raised when an edit could not be completed after bounded retries."""

    def __init__(self, message: str, code: str = "unknown") -> None:
        super().__init__(message)
        self.code = code


class MetaWikiPublisher:
    """:class:`blybot.domain.ports.WikiPublisher` backed by the MediaWiki API."""

    def __init__(  # noqa: PLR0913 -- configuration plus injectable test seams
        self,
        api_url: str,
        username: str,
        botpassword: str,
        user_agent: str,
        *,
        max_attempts: int = 5,
        counters: Counters | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._api_url = api_url
        self._username = username
        self._botpassword = botpassword
        self._max_attempts = max_attempts
        self._counters = counters or Counters()
        self._sleep = sleep
        self._csrf_token: str | None = None
        self._client = httpx.AsyncClient(
            headers={"User-Agent": user_agent},
            timeout=httpx.Timeout(30.0),
            transport=transport,
        )

    async def start_discussion(self, page: str, heading: str, text: str, summary: str) -> None:
        """Open a new section on ``page`` (``section=new`` is an atomic append)."""
        await self._submit_edit(
            {
                "title": page,
                "section": "new",
                "sectiontitle": heading,
                "text": text,
                "summary": summary,
            }
        )

    async def continue_discussion(self, page: str, heading: str, text: str, summary: str) -> None:
        """Append ``text`` inside the most recent section titled ``heading``.

        If the page or section does not exist — or vanishes between the
        lookup and the edit (archived mid-conversation) — the section is
        recreated, so a discussion can always continue somewhere. A
        lookup that *fails* (as opposed to finding nothing) raises
        instead: guessing "absent" on a transient error would fork the
        discussion into a duplicate section.
        """
        index = await self._find_section(page, heading)
        if index is None:
            await self.start_discussion(page, heading, text, summary)
            return
        try:
            await self._submit_edit(
                {
                    "title": page,
                    "section": index,
                    "appendtext": "\n" + text,
                    "summary": summary,
                }
            )
        except WikiPublishError as error:
            if error.code != "nosuchsection":
                raise
            # The section was archived between lookup and edit.
            log_event("wiki_section_recreated", "ok")
            await self.start_discussion(page, heading, text, summary)

    async def aclose(self) -> None:
        """Release the underlying HTTP client."""
        await self._client.aclose()

    async def _pause(self, attempt: int) -> None:
        """Capped exponential backoff before a retry; counts the retry."""
        self._counters.increment("api_retries")
        await self._sleep(min(2.0**attempt, 16.0))

    async def _submit_edit(self, edit_params: dict[str, str]) -> None:
        """Perform one edit with login, retry, and backoff handling (R8)."""
        self._counters.increment("publishes_attempted")
        last_error = "unknown"
        for attempt in range(self._max_attempts):
            if attempt:
                log_event("wiki_edit", "retry", attempt=attempt)
                await self._pause(attempt)
            try:
                data = await self._edit(edit_params)
            except httpx.HTTPError:
                last_error = "http"
                continue

            error_code = data.get("error", {}).get("code")
            if error_code is None and data.get("edit", {}).get("result") == "Success":
                self._counters.increment("publishes_succeeded")
                log_event("wiki_edit", "ok", attempts=attempt + 1)
                return
            last_error = str(error_code or "malformed-response")
            if error_code == "badtoken":
                self._csrf_token = None
            elif error_code == "assertuserfailed":
                self._csrf_token = None
                await self._login()
            elif error_code not in _RETRYABLE_API_CODES:
                break

        self._counters.increment("publishes_failed")
        log_event("wiki_edit", "error")
        msg = f"edit failed after retries: {last_error}"
        raise WikiPublishError(msg, code=last_error)

    async def _find_section(self, page: str, heading: str) -> str | None:
        """Return the edit index of the last section titled ``heading``, if any.

        New sections are always appended at the end of a page, so the
        index of an existing section is stable against concurrent
        discussions starting. Transient HTTP failures are retried with
        the same backoff as edits and raise after the attempt budget —
        they must never be mistaken for "section absent".
        """
        data: dict[str, Any] | None = None
        for attempt in range(self._max_attempts):
            if attempt:
                await self._pause(attempt)
            try:
                data = await self._post(action="parse", page=page, prop="sections")
                break
            except httpx.HTTPError:
                continue
        if data is None:
            msg = "section lookup failed after retries"
            raise WikiPublishError(msg, code="http")
        if data.get("error", {}).get("code") == "missingtitle":
            return None  # page not created yet: genuinely absent
        matches = [
            str(section.get("index"))
            for section in data.get("parse", {}).get("sections", [])
            if section.get("line") == heading and str(section.get("index", "")).isdigit()
        ]
        return matches[-1] if matches else None

    async def _edit(self, edit_params: dict[str, str]) -> dict[str, Any]:
        token = await self._ensure_csrf_token()
        return await self._post(
            action="edit",
            token=token,
            bot="1",
            maxlag=_MAXLAG_SECONDS,
            **{"assert": "user"},
            **edit_params,
        )

    async def _ensure_csrf_token(self) -> str:
        if self._csrf_token is None:
            token = await self._fetch_csrf_token()
            if token == _ANONYMOUS_TOKEN:  # we are not logged in (yet)
                await self._login()
                token = await self._fetch_csrf_token()
            if token == _ANONYMOUS_TOKEN:
                msg = "could not obtain a CSRF token; check credentials"
                raise WikiPublishError(msg)
            self._csrf_token = token
        return self._csrf_token

    async def _fetch_csrf_token(self) -> str:
        data = await self._post(action="query", meta="tokens", type="csrf")
        return str(data.get("query", {}).get("tokens", {}).get("csrftoken", _ANONYMOUS_TOKEN))

    async def _login(self) -> None:
        token_data = await self._post(action="query", meta="tokens", type="login")
        login_token = token_data.get("query", {}).get("tokens", {}).get("logintoken")
        if not login_token:
            msg = "could not obtain a login token"
            raise WikiPublishError(msg)
        result = await self._post(
            action="login",
            lgname=self._username,
            lgpassword=self._botpassword,
            lgtoken=str(login_token),
        )
        if result.get("login", {}).get("result") != "Success":
            log_event("wiki_login", "error")
            msg = "BotPassword login failed"
            raise WikiPublishError(msg)
        log_event("wiki_login", "ok")

    async def _post(self, **params: str) -> dict[str, Any]:
        response = await self._client.post(self._api_url, data={**params, "format": "json"})
        response.raise_for_status()
        payload: dict[str, Any] = response.json()
        return payload
