"""Analysis pipeline components: archive windows → prompts/stats → pages.

The action-framework components behind ``/summarize``, ``/talkingpoints``
and ``/stats`` (v3 plan §2.3, §2.5): a source that reads a scope's
archive window, a transform that runs a prompt template through the
:class:`~blybot.domain.ports.PromptRunner` with map-reduce chunking, a
pure-Python stats transform, and the wiki/Telegram sinks. The
publish-nothing invariant lives here: any contract violation aborts the
run with an :class:`~blybot.domain.ports.ActionError` before a sink is
reached.
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Final

from blybot.domain import prompts
from blybot.domain.models import (
    AnalysisReport,
    LlmSettings,
    OutboundMessage,
    PromptRequest,
    StatsReport,
    Transcript,
)
from blybot.domain.ports import ActionError, PromptError, StorageError
from blybot.domain.prompts import PromptContractError
from blybot.observability import log_event

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Mapping

    from blybot.domain.models import ActionContext, CapturedMessage, PromptResult, StepSpec
    from blybot.domain.ports import (
        MessageArchive,
        ProfileStore,
        PromptRunner,
        Sanitizer,
        WikiPublisher,
    )
    from blybot.observability import Counters
    from blybot.services.directory import ChannelDirectory

_WINDOW_RE: Final = re.compile(r"^(\d+)([hd])$")
MAX_WINDOW: Final = timedelta(days=30)
DEFAULT_WINDOW: Final = "24h"
_TOP_AUTHORS: Final = 5
_REPLY_CAP_CHARS: Final = 3500  # stay under Telegram's 4096 with margin
_ABORT: Final = "The analysis failed and nothing was published — try again later."


def parse_window(token: str) -> timedelta:
    """Parse ``24h`` / ``7d`` into a bounded timedelta."""
    match = _WINDOW_RE.match(token)
    if not match:
        msg = f"Window must look like 24h or 7d, got {token!r}"
        raise ActionError(msg)
    amount, unit = int(match.group(1)), match.group(2)
    window = timedelta(hours=amount) if unit == "h" else timedelta(days=amount)
    if not timedelta(0) < window <= MAX_WINDOW:
        msg = f"Window must be between 1h and {MAX_WINDOW.days}d"
        raise ActionError(msg)
    return window


@dataclass(eq=False)
class ArchiveWindowSource:
    """Reads the scope's archived messages for the configured window."""

    archive: MessageArchive

    async def fetch(self, context: ActionContext) -> Transcript | None:
        """Return the window's transcript, or ``None`` when it is empty."""
        window_arg = context.spec.source.param("window", DEFAULT_WINDOW)
        until = context.now
        since = self._since(context, window_arg, until)
        messages = await self.archive.window(
            context.scope.chat_id, context.scope.thread_id, since, until
        )
        if not messages:
            return None
        return Transcript(messages=tuple(messages), since=since, until=until)

    @staticmethod
    def _since(context: ActionContext, window_arg: str, until: datetime) -> datetime:
        if window_arg != "since_last_run":
            return until - parse_window(window_arg)
        # Watermark window: exactly-once over the archive for scheduled
        # digests. The scheduler stamps last_run to this run's `now`, so
        # the next window starts precisely where this one ends.
        if context.spec.last_run is None:
            msg = "window=since_last_run only works on scheduled actions"
            raise ActionError(msg)
        return max(context.spec.last_run, until - MAX_WINDOW)


@dataclass(eq=False)
class PromptTransform:
    """Runs a named template over the payload via the PromptRunner.

    Accepts a :class:`Transcript` (chunked map-reduce over its lines) or
    a :class:`StatsReport` (single call over the computed numbers —
    never raw content). Aborts — publishing nothing — on truncation,
    malformed output, or transport failure beyond one retry.
    """

    runners: Mapping[str, PromptRunner]  # platform name → adapter
    store: ProfileStore | None
    defaults: LlmSettings
    max_tokens_ceiling: int
    max_chunks: int
    counters: Counters
    chunk_chars: int = prompts.DEFAULT_CHUNK_CHARS

    async def apply(
        self, context: ActionContext, step: StepSpec, payload: object
    ) -> AnalysisReport:
        """Return the structured analysis; raise :class:`ActionError` on failure."""
        template = self._template(step)
        settings = await self._settings(context, step)
        if isinstance(payload, Transcript):
            hits = prompts.suspicion_count(m.text for m in payload.messages)
            if hits:
                # Telemetry only, never a gate: legitimate discussion
                # *about* prompt injection must still be analyzable.
                self.counters.increment("injection_suspected", hits)
                log_event("injection_heuristic", "ignored", hits=hits)
        lines, counts = _payload_lines(payload)
        chunks = prompts.chunk_lines(lines, self.chunk_chars)
        sampled = len(chunks) > self.max_chunks
        if sampled:
            log_event("analysis", "ignored", dropped_chunks=len(chunks) - self.max_chunks)
            chunks = chunks[: self.max_chunks]
        try:
            items = await self._map_reduce(template, settings, chunks)
        except (PromptError, PromptContractError) as error:
            self.counters.increment("analyses_aborted")
            log_event("analysis", "error")
            raise ActionError(_ABORT) from error
        message_count, participant_count, since, until = counts
        return AnalysisReport(
            title=template.title,
            items=items,
            message_count=message_count,
            participant_count=participant_count,
            since=since,
            until=until,
            model_label=f"{settings.model} model on {settings.platform}",
            lang=settings.lang,
            sampled=sampled,
        )

    def _template(self, step: StepSpec) -> prompts.PromptTemplate:
        name = step.param("template")
        try:
            return prompts.template_for(name)
        except KeyError as error:
            msg = f"Unknown prompt template {name!r}. Templates: {error.args[0]}"
            raise ActionError(msg) from error

    async def _settings(self, context: ActionContext, step: StepSpec) -> LlmSettings:
        settings = self.defaults
        if self.store is not None:
            try:
                profile = await self.store.get(context.scope.chat_id, context.scope.thread_id)
                # Topic override → group default → operator default: a forum
                # topic without its own /llm settings inherits thread 0's,
                # the same two-tier resolution pages and capture follow.
                if (profile is None or profile.llm is None) and context.scope.thread_id:
                    profile = await self.store.get(context.scope.chat_id, 0)
            except StorageError:
                profile = None  # analyses still run on deployment defaults
            if profile is not None and profile.llm is not None:
                settings = profile.llm
        if model := step.param("model"):
            settings = replace(settings, model=model)
        if lang := step.param("lang"):
            settings = replace(settings, lang=lang)
        if temp := step.param("temp"):
            settings = replace(settings, temperature=float(temp))
        return settings

    async def _map_reduce(
        self, template: prompts.PromptTemplate, settings: LlmSettings, chunks: list[str]
    ) -> tuple[str, ...]:
        fence = prompts.mint_fence()
        partials: list[str] = []
        for chunk in chunks:
            content = prompts.map_prompt(template, settings.lang, fence, chunk)
            partials.extend(await self._call(settings, content))
        if len(chunks) == 1:
            return tuple(partials)
        content = prompts.reduce_prompt(template, settings.lang, fence, partials)
        return tuple(await self._call(settings, content))

    def _runner(self, settings: LlmSettings) -> PromptRunner:
        runner = self.runners.get(settings.platform)
        if runner is None:
            known = ", ".join(sorted(self.runners))
            msg = f"unknown platform {settings.platform!r} (this deployment has: {known})"
            raise ActionError(msg)
        return runner

    async def _call(self, settings: LlmSettings, user_content: str) -> tuple[str, ...]:
        runner = self._runner(settings)
        request = PromptRequest(
            model=settings.model,
            system=prompts.system_prompt(settings.lang),
            user_content=user_content,
            max_tokens=min(settings.max_tokens, self.max_tokens_ceiling),
            temperature=settings.temperature,
        )
        result = await runner.run(request)
        try:
            return self._accept(result)
        except PromptContractError:
            # One retry with the shape restated even harder; a second
            # violation aborts the analysis (publish-nothing invariant).
            retry = replace(
                request,
                user_content=(
                    f"{user_content}\n\nYour previous reply violated the "
                    "format. Reply with ONLY the JSON array, no prose, no "
                    "code fences, and finish within the length limit."
                ),
            )
            return self._accept(await runner.run(retry))

    @staticmethod
    def _accept(result: PromptResult) -> tuple[str, ...]:
        if result.finish_reason == "length":
            msg = "output was truncated"
            raise PromptContractError(msg)
        return prompts.parse_items(result.content)


@dataclass(eq=False)
class StatsTransform:
    """Deterministic activity statistics — pure Python, no LLM."""

    async def apply(self, context: ActionContext, step: StepSpec, payload: object) -> StatsReport:
        """Compute a :class:`StatsReport` from the window's transcript."""
        del context, step
        if not isinstance(payload, Transcript):
            msg = "stats needs an archive window to work on"
            raise ActionError(msg)
        messages = payload.messages
        authors = Counter(_author_label(m) for m in messages)
        hours = Counter(m.posted_at.hour for m in messages)
        busiest = hours.most_common(1)
        return StatsReport(
            message_count=len(messages),
            participant_count=len(authors),
            media_count=sum(1 for m in messages if m.kind == "media_note"),
            reply_count=sum(1 for m in messages if m.reply_to is not None),
            busiest_hour_utc=busiest[0][0] if busiest else None,
            busiest_hour_count=busiest[0][1] if busiest else 0,
            top_authors=tuple(authors.most_common(_TOP_AUTHORS)),
            since=payload.since,
            until=payload.until,
        )


def explicit_page_resolver(
    directory: ChannelDirectory,
) -> Callable[[int, int], Awaitable[str]]:
    """The wiki sink's page policy: publish only to an explicitly set page.

    Same rule as ``/log`` on self-service deployments — a scope that
    never ran ``/setpage`` is refused (with the fix spelled out) rather
    than silently writing to the operator default page.
    """

    async def resolve_page(chat_id: int, thread_id: int) -> str:
        settings = await directory.resolve(chat_id, thread_id)
        if not settings.page_explicit:
            msg = (
                "No target page is set for this chat. An admin must "
                "run /setpage first, or add page=<title> to the action."
            )
            raise ActionError(msg)
        return settings.log_page

    return resolve_page


@dataclass(eq=False)
class WikiSectionSink:
    """Publishes a report as one new section on the scope's page."""

    publisher: WikiPublisher
    sanitizer: Sanitizer
    resolve_page: Callable[[int, int], Awaitable[str]]
    page_url_for: Callable[[str], str]
    edit_summary: str
    bot_name: str

    async def deliver(self, context: ActionContext, payload: object) -> tuple[OutboundMessage, ...]:
        """Render with trusted markup (fields sanitized) and publish."""
        report = _as_report(payload)
        page = context.spec.sink.param("page") or await self.resolve_page(
            context.scope.chat_id, context.scope.thread_id
        )
        heading = f"{context.now:%Y-%m-%d} — {_title(report)}"
        body = self._render(report)
        await self.publisher.start_discussion(page, heading, body, self.edit_summary)
        confirmation = OutboundMessage(
            chat_id=context.scope.chat_id,
            thread_id=context.scope.thread_id,
            text=f"Published: {_title(report)} → {self.page_url_for(page)}",
        )
        return (confirmation,)

    def _render(self, report: AnalysisReport | StatsReport) -> str:
        clean = self.sanitizer.sanitize
        scope_line = _scope_line(report)
        if isinstance(report, StatsReport):
            lines = [f"''{scope_line}''", ""]
            lines += [f"* {clean(line)}" for line in _stats_lines(report)]
            return "\n".join(lines)
        lines = [f"''{scope_line}''", ""]
        lines += [f"* {clean(item)}" for item in report.items]
        if report.sampled:
            lines.append("* ''(window truncated: only the earliest part was analyzed)''")
        lines += [
            "",
            f"''Generated by {self.bot_name} using the {report.model_label}. "
            "Machine-generated content — verify against the discussion before citing.''",
        ]
        return "\n".join(lines)


@dataclass(eq=False)
class TelegramReplySink:
    """Returns the report as chat text for the transport to send."""

    async def deliver(self, context: ActionContext, payload: object) -> tuple[OutboundMessage, ...]:
        """Render the report as one bounded chat message."""
        report = _as_report(payload)
        if isinstance(report, StatsReport):
            body = "\n".join(f"• {line}" for line in _stats_lines(report))
        else:
            body = "\n".join(f"• {item}" for item in report.items)
        text = f"{_title(report)} ({_scope_line(report)}):\n{body}"
        if len(text) > _REPLY_CAP_CHARS:
            text = f"{text[:_REPLY_CAP_CHARS]}…"
        message = OutboundMessage(
            chat_id=context.scope.chat_id, thread_id=context.scope.thread_id, text=text
        )
        return (message,)


def _payload_lines(payload: object) -> tuple[list[str], tuple[int, int, datetime, datetime]]:
    if isinstance(payload, Transcript):
        participants = {_author_label(m) for m in payload.messages}
        counts = (len(payload.messages), len(participants), payload.since, payload.until)
        return prompts.render_lines(payload.messages), counts
    if isinstance(payload, StatsReport):
        counts = (
            payload.message_count,
            payload.participant_count,
            payload.since,
            payload.until,
        )
        return _stats_lines(payload), counts
    msg = "prompt analyses need an archive window (or stats) to work on"
    raise ActionError(msg)


def _as_report(payload: object) -> AnalysisReport | StatsReport:
    if isinstance(payload, AnalysisReport | StatsReport):
        return payload
    msg = "this sink needs an analysis or stats report to publish"
    raise ActionError(msg)


def _title(report: AnalysisReport | StatsReport) -> str:
    return report.title if isinstance(report, AnalysisReport) else "Activity statistics"


def _scope_line(report: AnalysisReport | StatsReport) -> str:
    window = f"{report.since:%Y-%m-%d %H:%M} → {report.until:%Y-%m-%d %H:%M} UTC"
    return f"{report.message_count} messages from {report.participant_count} participants, {window}"


def _stats_lines(report: StatsReport) -> list[str]:
    lines = [
        f"messages: {report.message_count}",
        f"participants: {report.participant_count}",
        f"media posts: {report.media_count}",
        f"replies: {report.reply_count}",
    ]
    if report.busiest_hour_utc is not None:
        lines.append(
            f"busiest hour: {report.busiest_hour_utc:02d}:00 UTC "
            f"({report.busiest_hour_count} messages)"
        )
    if report.top_authors:
        top = ", ".join(f"{label} ({count})" for label, count in report.top_authors)
        lines.append(f"most active: {top}")
    return lines


def _author_label(message: CapturedMessage) -> str:
    return message.author or "channel"
