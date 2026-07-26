#!/usr/bin/env python3
"""Measure LiftWing chat-model latency from wherever this runs (v3 Phase 0.1).

Run it once from a Toolforge bastion (or a webservice shell) before
enabling scheduled analyses, and record the numbers in the "LiftWing LLM
endpoints" section of docs/OPERATIONS.md:

    python3 scripts/liftwing_baseline.py
    python3 scripts/liftwing_baseline.py --runs 5 --long-tokens 1024

Stdlib only — no venv required. Anonymous endpoint — no credentials. The
prompt is fixed and non-sensitive; nothing about your deployment is sent.
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
import urllib.error
import urllib.request

DEFAULT_BASE = "https://api.wikimedia.org/service/lw/inference/v1"
DEFAULT_MODELS = ("llm-qwen3-14b", "llm-qwen36-27b")
PROMPT = (
    "Summarize in exactly three short bullet points why community norms "
    "matter for collaborative wiki projects. Answer as a JSON array of "
    "three strings and nothing else."
)


def call(base: str, model: str, max_tokens: int, timeout: float) -> dict[str, object]:
    """One chat completion; returns latency and response metadata."""
    url = f"{base}/models/{model}/openai/v1/chat/completions"
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": PROMPT}],
        "max_tokens": max_tokens,
        "temperature": 0.2,
    }
    request = urllib.request.Request(  # noqa: S310 -- fixed https base
        url,
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json", "User-Agent": "blybot-baseline/1.0"},
    )
    started = time.monotonic()
    with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310
        body = json.load(response)
    elapsed = time.monotonic() - started
    choice = body["choices"][0]
    usage = body.get("usage", {})
    return {
        "seconds": elapsed,
        "finish_reason": choice.get("finish_reason"),
        "prompt_tokens": usage.get("prompt_tokens"),
        "completion_tokens": usage.get("completion_tokens"),
    }


def benchmark(base: str, model: str, runs: int, max_tokens: int, timeout: float) -> None:
    """Run and report one model's short-answer latency envelope."""
    print(f"\n{model}  ({runs} runs, max_tokens={max_tokens})")
    times: list[float] = []
    for attempt in range(1, runs + 1):
        try:
            result = call(base, model, max_tokens, timeout)
        except (urllib.error.URLError, TimeoutError, KeyError) as error:
            print(f"  run {attempt}: FAILED — {error}")
            continue
        times.append(float(str(result["seconds"])))
        print(
            f"  run {attempt}: {result['seconds']:.1f}s"
            f"  finish={result['finish_reason']}"
            f"  prompt={result['prompt_tokens']}t"
            f"  completion={result['completion_tokens']}t"
        )
    if times:
        print(
            f"  → median {statistics.median(times):.1f}s"
            f"  min {min(times):.1f}s  max {max(times):.1f}s"
        )
    else:
        print("  → every run failed; check connectivity and the model id")


def main() -> int:
    """CLI entry point."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", default=DEFAULT_BASE, help="LiftWing inference base URL")
    parser.add_argument("--models", nargs="*", default=list(DEFAULT_MODELS))
    parser.add_argument("--runs", type=int, default=3, help="short-answer runs per model")
    parser.add_argument("--long-tokens", type=int, default=512, help="long-generation probe size")
    parser.add_argument("--timeout", type=float, default=180.0, help="per-request timeout (s)")
    args = parser.parse_args()

    print(f"LiftWing baseline against {args.base}")
    for model in args.models:
        benchmark(args.base, model, args.runs, max_tokens=256, timeout=args.timeout)
        print(f"\n{model}  (1 long-generation probe, max_tokens={args.long_tokens})")
        benchmark(args.base, model, 1, max_tokens=args.long_tokens, timeout=args.timeout)
    print(
        "\nRecord the medians and the long-generation behavior in "
        "docs/OPERATIONS.md ('LiftWing LLM endpoints') and size "
        "LIFTWING_TIMEOUT_SECONDS above the observed max."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
