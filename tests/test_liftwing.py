"""LiftWingClient tests against a scripted OpenAI-compatible endpoint."""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from blybot.adapters.llm.liftwing import LiftWingClient
from blybot.domain.models import PromptRequest
from blybot.domain.ports import PromptError
from blybot.observability import Counters

BASE = "https://api.example/service/lw/inference/v1"
MODELS = {"default": "llm-qwen3-14b", "large": "llm-qwen36-27b"}


class FakeLiftWing:
    """Scripted chat-completions endpoint."""

    def __init__(self) -> None:
        self.requests: list[dict[str, Any]] = []
        self.urls: list[str] = []
        self.faults: list[int | Exception] = []  # per-call status codes / transport errors
        self.content = '["an item"]'
        self.finish_reason = "stop"
        self.body_override: dict[str, Any] | None = None

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.urls.append(str(request.url))
        self.requests.append(json.loads(request.content))
        if self.faults:
            fault = self.faults.pop(0)
            if isinstance(fault, Exception):
                raise fault
            return httpx.Response(fault, json={"error": "scripted"})
        body = self.body_override or {
            "choices": [
                {"message": {"content": self.content}, "finish_reason": self.finish_reason}
            ],
            "usage": {"prompt_tokens": 41, "completion_tokens": 12},
        }
        return httpx.Response(200, json=body)


def make_client(fake: FakeLiftWing, max_attempts: int = 3) -> tuple[LiftWingClient, Counters]:
    counters = Counters()
    naps: list[float] = []

    async def no_sleep(seconds: float) -> None:
        naps.append(seconds)

    client = LiftWingClient(
        api_base=BASE + "/",  # trailing slash must not double up
        user_agent="blybot-tests",
        models=MODELS,
        max_attempts=max_attempts,
        counters=counters,
        transport=httpx.MockTransport(fake.handler),
        sleep=no_sleep,
    )
    return client, counters


def request(model: str = "default") -> PromptRequest:
    return PromptRequest(
        model=model, system="sys", user_content="user", max_tokens=64, temperature=0.3
    )


async def test_runs_a_completion_with_the_resolved_model_id() -> None:
    fake = FakeLiftWing()
    client, counters = make_client(fake)

    result = await client.run(request("large"))

    assert result.content == '["an item"]'
    assert result.finish_reason == "stop"
    assert result.prompt_tokens == 41
    assert result.completion_tokens == 12
    (url,) = fake.urls
    assert url == f"{BASE}/models/llm-qwen36-27b/openai/v1/chat/completions"
    (payload,) = fake.requests
    assert payload["model"] == "llm-qwen36-27b"
    assert payload["messages"][0] == {"role": "system", "content": "sys"}
    assert payload["max_tokens"] == 64
    assert payload["temperature"] == 0.3
    snapshot = counters.snapshot()
    assert snapshot["prompts_run"] == 1
    assert snapshot["prompt_tokens"] == 41
    await client.aclose()


async def test_unknown_alias_fails_fast_and_names_the_choices() -> None:
    client, _counters = make_client(FakeLiftWing())
    with pytest.raises(PromptError, match="default, large"):
        await client.run(request("qwen99"))
    await client.aclose()


async def test_retries_on_429_and_5xx_then_succeeds() -> None:
    fake = FakeLiftWing()
    fake.faults = [429, 503]
    client, _counters = make_client(fake)

    result = await client.run(request())

    assert result.content == '["an item"]'
    assert len(fake.urls) == 3
    await client.aclose()


async def test_transport_errors_retry_then_exhaust_as_prompt_error() -> None:
    fake = FakeLiftWing()
    fake.faults = [httpx.ConnectError("down"), 502, 429]
    client, counters = make_client(fake, max_attempts=3)

    with pytest.raises(PromptError, match="after 3 attempts"):
        await client.run(request())
    assert counters.snapshot()["prompt_failures"] == 1
    await client.aclose()


async def test_client_errors_fail_fast_without_retry() -> None:
    fake = FakeLiftWing()
    fake.faults = [400]
    client, counters = make_client(fake)

    with pytest.raises(PromptError, match="HTTP 400"):
        await client.run(request())
    assert len(fake.urls) == 1  # no retry burned on a non-retryable status
    assert counters.snapshot()["prompt_failures"] == 1
    await client.aclose()


async def test_unexpected_response_shape_is_a_prompt_error() -> None:
    fake = FakeLiftWing()
    fake.body_override = {"unexpected": True}
    client, _counters = make_client(fake)

    with pytest.raises(PromptError, match="unexpected response shape"):
        await client.run(request())
    await client.aclose()


async def test_missing_usage_and_finish_reason_default_safely() -> None:
    fake = FakeLiftWing()
    fake.body_override = {"choices": [{"message": {"content": "ok"}, "finish_reason": None}]}
    client, _counters = make_client(fake)

    result = await client.run(request())

    assert result.finish_reason == "stop"
    assert result.prompt_tokens == 0
    await client.aclose()
