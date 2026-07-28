"""The neutral on-demand analysis-run service (issue #41, contract-first).

Both the Telegram and Discord adapters expose the same on-demand analyses
(``/summarize``, ``/talkingpoints``, ``/stats``, and Telegram's ``/run``):
build a one-shot :class:`~blybot.domain.models.ActionSpec` from the
command and run it through the :class:`~blybot.services.engine.ActionEngine`,
publishing the result to the scope's wiki page. This service holds that
run logic once, so each adapter only maps its native trigger to a neutral
call and renders the returned :class:`~blybot.domain.models.CommandResult`
(issue #31's contract-first rule — logic lives once, adapters map).

The reply wording lives here as module constants: one neutral phrasing per
outcome, nothing platform-specific (no Telegram "chat"/"topic", no Discord
"server"), so both adapters send them verbatim. The "analysing…" progress
message is *not* one of these: it is a platform affordance (Telegram sends
it as a pre-message; Discord defers the interaction instead), so the
adapter supplies it via the optional ``on_started`` hook, invoked once the
run is committed to — after the admit and throttle gates and a clean parse,
exactly where the long inference begins.

Scope is on-demand only. Scheduled analyses run through the same engine on
the subscription/schedule tick and are out of scope here (they need the
``/action`` config surface, issue #43).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Final

from blybot.domain.models import CommandResult
from blybot.domain.ports import ActionError
from blybot.observability import log_event
from blybot.services.actions import ActionParseError, command_action

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from blybot.domain.models import Scope
    from blybot.domain.ports import Clock
    from blybot.observability import Counters
    from blybot.services.engine import ActionEngine
    from blybot.services.policy import SlidingWindowLimiter

REPLY_NOT_ADMIN: Final = "Only this chat's admins can run analyses."
REPLY_THROTTLED: Final = "Too many analyses recently — try again later."
REPLY_FAILED: Final = "The analysis failed and nothing was published — try again later."
REPLY_EMPTY: Final = "Nothing to analyze: the archive has no messages in that window."


@dataclass(eq=False)
class AnalysisService:
    """Neutral run logic behind the on-demand analysis commands.

    Holds only neutral dependencies: the :class:`ActionEngine` that runs the
    pipeline, the :class:`SlidingWindowLimiter` that throttles per channel,
    the :class:`~blybot.domain.ports.Clock`, and the operator ``counters``.
    """

    engine: ActionEngine
    limiter: SlidingWindowLimiter
    clock: Clock
    counters: Counters

    async def run_analysis(  # noqa: PLR0911, PLR0913 -- one branch per neutral outcome
        self,
        scope: Scope,
        *,
        is_admin: bool,
        command: str,
        recipe: str,
        tokens: list[str],
        on_started: Callable[[], Awaitable[None]] | None = None,
    ) -> CommandResult:
        """Run one on-demand analysis; return its ready-to-send outcome.

        ``on_started`` (when given) is awaited once the run is committed to —
        after the admit + throttle gates and a clean parse, but before the
        engine runs — so an adapter can announce "analysing…" only when the
        long inference is actually about to start (never on a refusal or a
        throttle). The confirmation with the published page URL is
        ``outcome.messages[0]``; the ``ok=False`` results are refusals,
        parse errors, or a failed run.
        """
        if not is_admin:
            return CommandResult(REPLY_NOT_ADMIN, ok=False)
        # Per-channel throttle: analyses cost inference calls and publish
        # publicly, so one busy channel cannot monopolise the deployment.
        if not self.limiter.allow("analysis", int(scope.channel)):
            return CommandResult(REPLY_THROTTLED, ok=False)
        try:
            spec = command_action(command, recipe, tokens)
        except ActionParseError as error:
            return CommandResult(str(error), ok=False)
        if on_started is not None:
            await on_started()
        try:
            outcome = await self.engine.run(scope, spec, self.clock.now())
        except ActionError as error:
            # A configuration/contract abort (unknown component, no target
            # page, bad window): the message is user-facing, show it verbatim.
            return CommandResult(str(error), ok=False)
        except Exception:
            # Adapter failures (wiki write, storage) after retries: tell the
            # admin plainly, count and log it operationally. Deliberately
            # broad — a command must never let an exception escape.
            self.counters.increment("analyses_failed")
            log_event("analysis_command", "error")
            return CommandResult(REPLY_FAILED, ok=False)
        if not outcome.messages:
            return CommandResult(REPLY_EMPTY, ok=False)
        return CommandResult(outcome.messages[0].text)
