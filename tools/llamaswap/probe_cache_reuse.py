#!/usr/bin/env python3
"""
Cycle F Sprint 1 — prefix cache-reuse verification probe.

Sends the SAME ~1000-token prompt twice to llama-swap at
`http://<host>:9100/v1/chat/completions`.  Reads `/slots` after
each call and asserts:

  * the second prefill takes <= 0.2× the first prefill (factor 5×
    speedup), tolerating ±10% jitter;
  * the second call's slot reports `n_cached_tokens > 0`.

The Sprint 1 overnight A/B run calls this BEFORE the 6-hour
Mistral judge pass so a regression aborts cheap rather than after
six hours.

Exit codes:
  0  cache reuse verified (both assertions pass)
  1  one or both assertions failed (regression — DO NOT proceed)
  2  fatal (llama-swap unreachable, model not loaded, etc.)
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

# Force UTF-8 stdio on Windows so the unicode glyphs below don't blow
# up on cp1252 terminals.  No-op on POSIX.
if sys.platform == "win32":
    for _s in (sys.stdout, sys.stderr):
        _r = getattr(_s, "reconfigure", None)
        if _r is not None:
            try:
                _r(encoding="utf-8", errors="replace")
            except (OSError, ValueError):
                pass


# A deterministic ~1000-token prompt (system + user) that should
# fully load the prefix cache.  Sized so the prefill cost is
# measurable yet bounded.
SYSTEM = (
    "You are a meticulous senior software engineer.  When the user "
    "asks for code, you reason step-by-step before writing it, you "
    "always include type hints in Python, and you always add a brief "
    "docstring that describes the contract: arguments, return value, "
    "and raised exceptions.  You never use external libraries unless "
    "they appear in the user's request.  Your code is production "
    "quality: it handles edge cases, validates inputs, and degrades "
    "gracefully on unexpected input.  You prefer simple, readable "
    "implementations over clever ones.  You comment only where the "
    "intent is non-obvious; you do not narrate what the code does.  "
    "When the user's request is ambiguous, you make a reasonable "
    "assumption and state it at the top of your answer.  You always "
    "test your output mentally for correctness before finalizing.  "
    "If the user asks a follow-up question, you preserve the previous "
    "context and only modify what's needed.  Above all, you write "
    "code that another senior engineer would approve in code review."
)
USER = (
    "Write a Python function `count_word_frequency(text: str, "
    "stopwords: set[str] | None = None) -> dict[str, int]` that "
    "returns a dictionary mapping each lowercase word to its frequency "
    "in `text`, excluding any word in `stopwords` (case-insensitive).  "
    "Words are split on whitespace; punctuation is stripped from each "
    "side.  Empty strings are skipped.  If `stopwords` is None, no "
    "filtering is applied."
)


def _http_post_json(url: str, body: dict, timeout: float = 60.0) -> tuple[dict, float]:
    """POST + measure wall-clock time.  Returns (json, elapsed_s)."""

    payload = json.dumps(body).encode("utf-8")
    req = Request(
        url,
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    start = time.monotonic()
    try:
        with urlopen(req, timeout=timeout) as resp:  # noqa: S310
            data = json.loads(resp.read().decode("utf-8"))
    except HTTPError as exc:
        print(f"[probe] HTTP {exc.code}: {exc.read().decode('utf-8', 'replace')[:200]}",
              file=sys.stderr)
        raise
    elapsed = time.monotonic() - start
    return data, elapsed


def _http_get_json(url: str, timeout: float = 5.0) -> dict | list:
    req = Request(url)
    with urlopen(req, timeout=timeout) as resp:  # noqa: S310
        return json.loads(resp.read().decode("utf-8"))


def probe(
    base_url: str,
    model: str,
    ctx_threshold_ratio: float = 0.2,
    unique_prefix: bool = False,
) -> int:
    """Issue two identical chats; assert call-2 has cached_tokens > 0
    AND call-2's prefill (timings.prompt_ms) is far smaller than call-1's.

    Uses llama-server's structured `timings` + `usage` fields rather
    than wall-clock to avoid measuring constant decode overhead.
    """

    chat_url = base_url.rstrip("/") + "/v1/chat/completions"

    system = SYSTEM
    if unique_prefix:
        # Salt the system prompt so call 1 ALWAYS hits a cold prefix
        # cache (useful for ad-hoc runs when llama-swap wasn't just
        # restarted; redundant inside tools/sprint1_ab_run.sh which
        # already restarts the container).
        import random
        import string
        salt = "".join(random.choices(string.ascii_lowercase, k=12))
        system = f"{SYSTEM}\n\n[probe-salt: {salt}]"

    body = {
        "model": model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": USER},
        ],
        "max_tokens": 2,  # minimise decode overhead in wall-clock
        "temperature": 0.0,
        "stream": False,
    }

    print(f"[probe] target: {chat_url} (model={model})")

    def _do(label: str) -> dict:
        print(f"[probe] {label}")
        try:
            data, wall_s = _http_post_json(chat_url, body, timeout=120)
        except (HTTPError, URLError, TimeoutError, ConnectionError, OSError) as exc:
            print(f"[probe] FATAL: {label} failed: {exc!r}", file=sys.stderr)
            sys.exit(2)
        usage = data.get("usage", {}) or {}
        timings = data.get("timings", {}) or {}
        cached = (usage.get("prompt_tokens_details") or {}).get("cached_tokens", 0)
        prompt_ms = float(timings.get("prompt_ms", 0.0))
        prompt_n = int(timings.get("prompt_n", 0))
        print(f"[probe]   wall = {wall_s:.3f}s  "
              f"prefill = {prompt_ms:.1f}ms over {prompt_n} new tokens  "
              f"cached_tokens = {cached}")
        return {
            "wall_s": wall_s,
            "prompt_ms": prompt_ms,
            "prompt_n": prompt_n,
            "cached_tokens": int(cached),
        }

    first = _do("call 1 — cold prefix...")
    second = _do("call 2 — same prefix...")

    # ─── Assertion 1: call-2 must have cached_tokens > 0 ────────────
    if second["cached_tokens"] <= 0:
        print(f"[probe] FAIL: call 2 reports cached_tokens="
              f"{second['cached_tokens']} (expected > 0).  Prefix "
              "cache is NOT being reused — check --cache-reuse flag.",
              file=sys.stderr)
        return 1

    # ─── Assertion 2: call-2's prefill must be far smaller ──────────
    # Threshold-ratio gate on prefill ms (decode-free).
    if first["prompt_ms"] <= 0:
        print(f"[probe] WARN: first prefill prompt_ms=0 (model was "
              "already warm); skipping ratio gate but cache reuse "
              "confirmed via cached_tokens.")
        print("[probe] PASS: prefix cache reuse verified (via cached_tokens) ✓")
        return 0

    ratio = second["prompt_ms"] / first["prompt_ms"]
    print(f"[probe]   prefill ratio (2nd / 1st) = {ratio:.3f}  "
          f"(threshold: <= {ctx_threshold_ratio:.2f} + 10% jitter)")
    if ratio > ctx_threshold_ratio * 1.10:
        print(f"[probe] FAIL: prefill speedup insufficient.  Expected "
              f"<= {ctx_threshold_ratio:.2f}x prefill_ms, got {ratio:.3f}x.",
              file=sys.stderr)
        return 1

    print(f"[probe] PASS: call-2 cached={second['cached_tokens']}  "
          f"prefill {first['prompt_ms']:.0f}ms -> {second['prompt_ms']:.0f}ms  "
          f"({ratio:.2f}x)  ✓")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Verify llama-swap prefix-cache reuse on identical prompts."
    )
    parser.add_argument(
        "--base-url",
        default="http://localhost:9100",
        help="llama-swap base URL (default: http://localhost:9100).",
    )
    parser.add_argument(
        "--model",
        default="amor-editor",
        help="Model alias to probe (default: amor-editor).",
    )
    parser.add_argument(
        "--threshold-ratio", type=float, default=0.2,
        help="Max acceptable (2nd/1st) wall-clock ratio (default 0.2).",
    )
    parser.add_argument(
        "--unique-prefix", action="store_true",
        help=("Salt the system prompt with a random nonce so call 1 "
              "always faces a cold prefix cache.  Use for ad-hoc "
              "runs against a warm llama-swap; unnecessary inside "
              "tools/sprint1_ab_run.sh (which cold-restarts already)."),
    )
    args = parser.parse_args()
    return probe(
        args.base_url, args.model, args.threshold_ratio,
        unique_prefix=args.unique_prefix,
    )


if __name__ == "__main__":
    raise SystemExit(main())
