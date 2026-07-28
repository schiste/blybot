"""AnalysisService: on-demand run logic proven independent of any adapter.

Drives :class:`AnalysisService.run_analysis` directly with plain
:class:`Scope` arguments and in-memory engine fakes — no Telegram or
Discord object in sight. Both adapters delegate here, so covering every
branch once is the "logic tested without an adapter" proof issue #41 asks
for.
"""

from __future__ import annotations

from datetime import timedelta

from blybot.domain.models import CommandResult, OutboundMessage, Scope
from blybot.domain.ports import ActionError
from blybot.observability import Counters
from blybot.services import analysis_run as ar
from blybot.services.analysis_run import AnalysisService
from blybot.services.engine import ActionEngine
from blybot.services.policy import SlidingWindowLimiter
from tests.fakes import FakeClock, FakeSink, FakeSource, SuffixTransform

_SCOPE = Scope("neutral", "555000111")
CONFIRMATION = OutboundMessage(scope=_SCOPE, text="Published: url")


def _service(
    *,
    source: FakeSource | None = None,
    sink: FakeSink | None = None,
    limit: int = 10,
) -> AnalysisService:
    clock = FakeClock()
    engine = ActionEngine(
        sources={"archive_window": source or FakeSource(payload="transcript")},
        transforms={"prompt": SuffixTransform(), "stats": SuffixTransform()},
        sinks={"wiki_section": sink or FakeSink(messages=(CONFIRMATION,))},
        counters=Counters(),
        clock=clock,
    )
    return AnalysisService(
        engine=engine,
        limiter=SlidingWindowLimiter(clock=clock, limit=limit, window=timedelta(hours=1)),
        clock=clock,
        counters=Counters(),
    )


async def _run(service: AnalysisService, **overrides: object) -> CommandResult:
    kwargs: dict[str, object] = {
        "is_admin": True,
        "command": "summarize",
        "recipe": "summarize",
        "tokens": [],
    }
    kwargs.update(overrides)
    return await service.run_analysis(_SCOPE, **kwargs)  # type: ignore[arg-type]


async def test_non_admins_are_refused() -> None:
    result = await _run(_service(), is_admin=False)
    assert result == CommandResult(ar.REPLY_NOT_ADMIN, ok=False)


async def test_analyses_are_throttled_per_channel() -> None:
    service = _service(limit=1)
    first = await _run(service, command="stats", recipe="stats")
    second = await _run(service, command="stats", recipe="stats")
    assert first.ok is True
    assert second == CommandResult(ar.REPLY_THROTTLED, ok=False)


async def test_a_parse_error_comes_back_verbatim() -> None:
    result = await _run(_service(), tokens=["bogus=1"])
    assert result.ok is False
    assert "Expected key=value" in result.text


async def test_action_errors_reach_the_caller_verbatim() -> None:
    class ExplodingSink:
        async def deliver(self, context: object, payload: object) -> tuple[()]:
            del context, payload
            msg = "No target page is set for this chat."
            raise ActionError(msg)

    service = _service()
    service.engine.sinks = {"wiki_section": ExplodingSink()}
    result = await _run(service)
    assert result == CommandResult("No target page is set for this chat.", ok=False)


async def test_unexpected_failures_reply_generically_and_count() -> None:
    service = _service(sink=FakeSink(fail=True))  # RuntimeError from the sink
    result = await _run(service)
    assert result == CommandResult(ar.REPLY_FAILED, ok=False)
    assert service.counters.snapshot()["analyses_failed"] == 1


async def test_empty_windows_get_a_quiet_notice() -> None:
    result = await _run(_service(source=FakeSource(payload=None)))
    assert result == CommandResult(ar.REPLY_EMPTY, ok=False)


async def test_success_returns_the_wiki_confirmation_without_a_progress_hook() -> None:
    result = await _run(_service())  # on_started defaults to None
    assert result == CommandResult("Published: url")


async def test_the_progress_hook_fires_after_parse_and_before_the_run() -> None:
    events: list[str] = []

    class RecordingSource:
        async def fetch(self, context: object) -> str:
            del context
            events.append("run")
            return "transcript"

    service = _service()
    service.engine.sources = {"archive_window": RecordingSource()}

    async def announce() -> None:
        events.append("announce")

    result = await _run(service, on_started=announce)
    assert result == CommandResult("Published: url")
    assert events == ["announce", "run"]  # committed to the run, then inference


async def test_the_progress_hook_is_skipped_on_a_parse_error() -> None:
    fired: list[int] = []

    async def announce() -> None:
        fired.append(1)

    result = await _run(_service(), tokens=["bogus=1"], on_started=announce)
    assert result.ok is False
    assert fired == []  # a bad parse never announces the run
