"""Wikimedia LiftWing platform: PromptRunner over chat completions (v3 §2.3).

Speaks LiftWing's OpenAI-compatible endpoint::

    POST {base}/models/{model}/openai/v1/chat/completions

No API key is required; calls from Toolforge are effectively
unthrottled. Model *aliases* (``default``/``large``) map to concrete ids
here, so operators re-point them in config without touching any scope's
settings. Retries are bounded with exponential backoff on 429/5xx and
transport errors; anything else fails fast as a :class:`PromptError`.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any, Final

import httpx

from blybot.domain.models import PromptResult
from blybot.domain.ports import PromptError
from blybot.observability import Counters, log_event

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Mapping

    from blybot.domain.models import PromptRequest

_RETRYABLE: Final = frozenset({429, 500, 502, 503, 504})


class LiftWingClient:
    """:class:`blybot.domain.ports.PromptRunner` backed by LiftWing."""

    def __init__(  # noqa: PLR0913 -- configuration plus injectable test seams
        self,
        api_base: str,
        user_agent: str,
        models: Mapping[str, str],
        *,
        timeout_seconds: float = 120.0,
        max_attempts: int = 4,
        counters: Counters | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._api_base = api_base.rstrip("/")
        self._models = dict(models)
        self._max_attempts = max_attempts
        self._counters = counters or Counters()
        self._sleep = sleep
        self._client = httpx.AsyncClient(
            headers={"User-Agent": user_agent},
            timeout=httpx.Timeout(timeout_seconds),
            transport=transport,
        )

    async def run(self, request: PromptRequest) -> PromptResult:
        """Execute one chat completion; raise :class:`PromptError` on failure."""
        model_id = self._models.get(request.model)
        if not model_id:
            known = ", ".join(sorted(self._models))
            msg = f"unknown model alias {request.model!r} (this deployment has: {known})"
            raise PromptError(msg)
        payload = {
            "model": model_id,
            "messages": [
                {"role": "system", "content": request.system},
                {"role": "user", "content": request.user_content},
            ],
            "max_tokens": request.max_tokens,
            "temperature": request.temperature,
        }
        url = f"{self._api_base}/models/{model_id}/openai/v1/chat/completions"
        data = await self._post_with_retries(url, payload)
        result = _parse_completion(data)
        self._counters.increment("prompt_tokens", result.prompt_tokens)
        self._counters.increment("completion_tokens", result.completion_tokens)
        self._counters.increment("prompts_run")
        return result

    async def aclose(self) -> None:
        """Release the HTTP client."""
        await self._client.aclose()

    async def _post_with_retries(self, url: str, payload: dict[str, Any]) -> dict[str, Any]:
        for attempt in range(1, self._max_attempts + 1):
            try:
                response = await self._client.post(url, json=payload)
            except httpx.HTTPError:
                log_event("prompt_call", "error", attempt=attempt)
            else:
                if response.status_code == httpx.codes.OK:
                    loaded: dict[str, Any] = response.json()
                    return loaded
                if response.status_code not in _RETRYABLE:
                    self._counters.increment("prompt_failures")
                    msg = f"inference endpoint answered HTTP {response.status_code}"
                    raise PromptError(msg)
                log_event("prompt_call", "error", status=response.status_code, attempt=attempt)
            if attempt < self._max_attempts:
                await self._sleep(2.0 ** (attempt - 1))
        self._counters.increment("prompt_failures")
        msg = f"inference endpoint unavailable after {self._max_attempts} attempts"
        raise PromptError(msg)


def _parse_completion(data: dict[str, Any]) -> PromptResult:
    try:
        choice = data["choices"][0]
        content = choice["message"]["content"]
    except (KeyError, IndexError, TypeError) as error:
        msg = "inference endpoint returned an unexpected response shape"
        raise PromptError(msg) from error
    usage = data.get("usage") or {}
    return PromptResult(
        content=content or "",
        finish_reason=choice.get("finish_reason") or "stop",
        prompt_tokens=int(usage.get("prompt_tokens") or 0),
        completion_tokens=int(usage.get("completion_tokens") or 0),
    )
