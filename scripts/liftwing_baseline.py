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
from typing import NamedTuple

DEFAULT_BASE = "https://api.wikimedia.org/service/lw/inference/v1"
DEFAULT_MODELS = ("llm-qwen3-14b", "llm-qwen36-27b")
SHORT_PROMPT = (
    "Summarize in exactly three short bullet points why community norms "
    "matter for collaborative wiki projects. Answer as a JSON array of "
    "three strings and nothing else."
)
# The long probe must actually consume the requested budget — an answer
# that stops early measures nothing. An open-ended enumeration reliably
# generates until max_tokens cuts it off (expect finish=length).
LONG_PROMPT = (
    "Write an exhaustive, numbered list of distinct considerations for "
    "moderating a large multilingual online community. For every item "
    "give a two-sentence explanation. Do not summarize or stop early; "
    "keep adding items until you run out of space."
)
# Production /summarize sends up to DEFAULT_CHUNK_CHARS (24,000 chars)
# of fenced transcript in one request; prefill latency at that size is
# part of the real envelope, so the baseline includes one such request.
CHUNK_LINE = (
    "[2026-07-25 12:{minute:02d}] {author}: we discussed the proposal about the "
    "upcoming edit-a-thon and whether the documentation page needs a translation "
    "sweep before the announcement goes out to the mailing list.\n"
)


def chunk_prompt(chars: int) -> str:
    """A production-shaped prompt: instruction + ~``chars`` of transcript."""
    lines: list[str] = []
    minute = 0
    while sum(len(line) for line in lines) < chars:
        author = f"anon{minute % 7:02d}"
        lines.append(CHUNK_LINE.format(minute=minute % 60, author=author))
        minute += 1
    transcript = "".join(lines)[:chars]
    return (
        "Summarize the main discussion threads in the following chat "
        f"transcript as a JSON array of short strings.\n\n{transcript}"
    )


class Probe(NamedTuple):
    """One benchmark shape: what to ask, how often, and how much to allow."""

    prompt: str
    runs: int
    max_tokens: int
    timeout: float


def call(base: str, model: str, probe: Probe) -> dict[str, object]:
    """One chat completion; returns latency and response metadata."""
    url = f"{base}/models/{model}/openai/v1/chat/completions"
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": probe.prompt}],
        "max_tokens": probe.max_tokens,
        "temperature": 0.2,
    }
    request = urllib.request.Request(  # noqa: S310 -- fixed https base
        url,
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json", "User-Agent": "blybot-baseline/1.0"},
    )
    started = time.monotonic()
    with urllib.request.urlopen(request, timeout=probe.timeout) as response:  # noqa: S310
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


def benchmark(base: str, model: str, probe: Probe) -> None:
    """Run and report one probe's latency envelope for one model."""
    print(f"\n{model}  ({probe.runs} runs, max_tokens={probe.max_tokens})")
    times: list[float] = []
    for attempt in range(1, probe.runs + 1):
        try:
            result = call(base, model, probe)
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
    parser.add_argument(
        "--long-tokens",
        type=int,
        default=4096,  # LLM_MAX_TOKENS_CEILING's default: measure the worst case
        help="long-generation probe size (match your LLM_MAX_TOKENS_CEILING)",
    )
    parser.add_argument(
        "--chunk-chars",
        type=int,
        default=24_000,  # the prompt transform's DEFAULT_CHUNK_CHARS
        help="transcript size for the production-shaped prefill probe",
    )
    parser.add_argument("--timeout", type=float, default=600.0, help="per-request timeout (s)")
    args = parser.parse_args()

    short = Probe(SHORT_PROMPT, runs=args.runs, max_tokens=256, timeout=args.timeout)
    chunk = Probe(chunk_prompt(args.chunk_chars), runs=1, max_tokens=1024, timeout=args.timeout)
    long_probe = Probe(LONG_PROMPT, runs=1, max_tokens=args.long_tokens, timeout=args.timeout)
    print(f"LiftWing baseline against {args.base}")
    for model in args.models:
        print(f"\n--- {model}: short answers ---")
        benchmark(args.base, model, short)
        print(f"\n--- {model}: production-shaped chunk ({args.chunk_chars} chars in) ---")
        benchmark(args.base, model, chunk)
        print(f"\n--- {model}: long generation (fills the {args.long_tokens}-token budget) ---")
        benchmark(args.base, model, long_probe)
    print(
        "\nA long probe should end with finish=length and completion≈the "
        "requested budget — if it stopped early, its timing is not a real "
        "long-generation measurement. Record the medians in "
        "docs/OPERATIONS.md ('LiftWing LLM endpoints') and size "
        "LIFTWING_TIMEOUT_SECONDS above the slowest observation here "
        "(defaults already match production: 24k-char chunks in, the "
        "4096-token ceiling out)."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
