#!/usr/bin/env python3
"""
Cycle G G5 — synthetic preference-pair generator (Day-1 contingency).

Plan-agent flagged HIGH-risk corpus famine: AMOR is single-user, so
accumulating 200 rated preference pairs per role via MessageActions
ratings alone takes weeks-to-months.  G5's LoRA adapter training
needs MIN_PAIRS pairs immediately to ship a first-pass adapter.

Solution: re-run the Sprint-0 corpus prompts through the active LLM
backend at TWO temperatures — temp=0.0 (chosen, deterministic high-
quality output) vs temp=0.7 (rejected, varied output that surfaces
the same prompt's mediocre completions).  The (chosen, rejected)
delta is the ORPO training signal.

Output is tagged ``synthetic=true`` in the JSONL so the trainer +
operator can filter it out / weight it lower than human-rated pairs.

Workflow
--------

  # 1. Generate 50 synthetic pairs from the Sprint-0 corpus
  python tools/training/synth_pair_generator.py \\
    --role coder \\
    --corpus tests/baselines/sprint0_prompts.json \\
    --out data/preference_pairs/coder_synth.jsonl \\
    --pairs-per-prompt 5

  # 2. (Optional) Merge with rated pairs from the cron export
  cat data/preference_pairs/coder_synth.jsonl \\
      data/preference_pairs/coder.jsonl \\
      > data/preference_pairs/coder_merged.jsonl

  # 3. Train
  python tools/training/orpo_role_adapter.py \\
    --role coder \\
    --jsonl data/preference_pairs/coder_merged.jsonl
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import logging
import os
import sys
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, List, Optional

import httpx

if sys.platform == "win32":
    for _s in (sys.stdout, sys.stderr):
        _r = getattr(_s, "reconfigure", None)
        if _r is not None:
            try:
                _r(encoding="utf-8", errors="replace")
            except (OSError, ValueError):
                pass

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
logger = logging.getLogger(__name__)


# ─── LLM endpoint resolution (shared pattern) ──────────────────────


def _llm_base_url() -> str:
    """Same falsy-skip pattern as humaneval_plus.py / swebench_lite.py
    (v18.1 Step 6 + v18.1.1 hotfix lessons)."""
    for key in ("AMOR_LLM_BACKEND_URL", "AMOR_LLAMASWAP_URL"):
        value = (os.environ.get(key) or "").strip()
        if value:
            return value
    return "http://amor-llama-swap:9100"


def _llm_model() -> str:
    return (os.environ.get("AMOR_SYNTH_MODEL") or "amor-editor").strip()


# ─── Per-prompt completion (deterministic + varied) ────────────────


async def _complete(
    client: httpx.AsyncClient,
    base_url: str,
    model: str,
    prompt: str,
    temperature: float,
    max_tokens: int = 1024,
    seed: Optional[int] = None,
    timeout_s: float = 60.0,
) -> str:
    body = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": temperature,
        "max_tokens": max_tokens,
        "stream": False,
    }
    if seed is not None:
        body["seed"] = int(seed)
    try:
        r = await client.post(
            f"{base_url}/v1/chat/completions",
            json=body, timeout=timeout_s,
        )
        r.raise_for_status()
        data = r.json()
        return (data.get("choices") or [{}])[0].get("message", {}).get("content", "") or ""
    except (httpx.HTTPError, KeyError, IndexError, ValueError) as exc:
        logger.warning("completion error temp=%.2f err=%s", temperature, exc)
        return ""


# ─── Synthesize one (chosen, rejected) pair ────────────────────────


async def synth_one_pair(
    client: httpx.AsyncClient,
    base_url: str,
    model: str,
    prompt: str,
    *,
    chosen_temp: float = 0.0,
    rejected_temp: float = 0.7,
    rejected_seed: Optional[int] = None,
) -> Dict[str, Any]:
    """Produce one (chosen, rejected) pair for the given prompt.

    Returns a dict matching the JSONL row shape consumed by
    `orpo_role_adapter.py`.  Both completions may be empty when the
    backend errors — the caller decides whether to skip.
    """
    chosen = await _complete(client, base_url, model, prompt, chosen_temp)
    rejected = await _complete(
        client, base_url, model, prompt, rejected_temp, seed=rejected_seed,
    )
    return {
        "prompt": prompt,
        "chosen": chosen,
        "rejected": rejected,
        "synthetic": True,
        "chosen_temp": chosen_temp,
        "rejected_temp": rejected_temp,
        "model": model,
        "hash": hashlib.sha256(
            (prompt + chosen + rejected).encode("utf-8", errors="replace"),
        ).hexdigest()[:32],
    }


# ─── Corpus loader ─────────────────────────────────────────────────


def _load_corpus(path: Path) -> List[str]:
    """Load prompts from a Sprint-0-style JSON file.  Supports two
    shapes:
      * ``{"prompts": [{"prompt": "..."}, ...]}`` — sprint0_prompts.json
      * ``[{"prompt": "..."}, ...]`` — plain list

    Also tolerates a flat list of strings.  Returns the prompt
    strings only.
    """
    if not path.is_file():
        return []
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        logger.error("corpus unparseable at %s: %s", path, exc)
        return []
    if isinstance(raw, dict):
        items = raw.get("prompts") or raw.get("items") or []
    elif isinstance(raw, list):
        items = raw
    else:
        return []
    prompts: List[str] = []
    for item in items:
        if isinstance(item, str):
            prompts.append(item)
        elif isinstance(item, dict):
            text = item.get("prompt") or item.get("text") or item.get("query")
            if text:
                prompts.append(str(text))
    return prompts


# ─── Main entry — generate N pairs per prompt ──────────────────────


async def generate_pairs(
    *,
    corpus_path: Path,
    out_path: Path,
    pairs_per_prompt: int = 5,
    base_url: Optional[str] = None,
    model: Optional[str] = None,
    chosen_temp: float = 0.0,
    rejected_temp: float = 0.7,
    max_prompts: Optional[int] = None,
) -> Dict[str, Any]:
    """Generate ``pairs_per_prompt`` pairs for every prompt in the
    corpus.  Appends JSONL lines to ``out_path`` (creates parent dir
    if missing).  Returns a summary dict.

    Idempotency-light: the function APPENDS rather than overwrites so
    you can re-run with different temperatures and grow the corpus.
    Use a fresh ``--out`` path if you want a clean slate.
    """
    base_url = base_url or _llm_base_url()
    model = model or _llm_model()

    prompts = _load_corpus(corpus_path)
    if max_prompts:
        prompts = prompts[:max_prompts]
    if not prompts:
        return {"ok": False, "error": f"no prompts loaded from {corpus_path}"}

    out_path.parent.mkdir(parents=True, exist_ok=True)

    total_pairs = 0
    failed = 0
    async with httpx.AsyncClient() as client:
        with out_path.open("a", encoding="utf-8") as fh:
            for prompt_idx, prompt in enumerate(prompts):
                for pair_idx in range(pairs_per_prompt):
                    seed = 1000 * (prompt_idx + 1) + pair_idx
                    row = await synth_one_pair(
                        client, base_url, model, prompt,
                        chosen_temp=chosen_temp,
                        rejected_temp=rejected_temp,
                        rejected_seed=seed,
                    )
                    # Skip pairs where one or both completions are empty
                    # (backend error) — no signal in an empty pair.
                    if not row["chosen"] or not row["rejected"]:
                        failed += 1
                        continue
                    fh.write(json.dumps(row, ensure_ascii=False) + "\n")
                    total_pairs += 1
                logger.info(
                    "[%d/%d] prompt synth complete; pairs_so_far=%d",
                    prompt_idx + 1, len(prompts), total_pairs,
                )

    return {
        "ok": True,
        "out_path": str(out_path),
        "prompts": len(prompts),
        "pairs_written": total_pairs,
        "pairs_failed": failed,
        "model": model,
        "endpoint": base_url,
    }


# ─── CLI ────────────────────────────────────────────────────────────


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--role", choices=("coder", "tester", "debugger"), default="coder",
                   help="role tag used as the default output filename")
    p.add_argument("--corpus", default="tests/baselines/sprint0_prompts.json",
                   help="path to a JSON file of prompts")
    p.add_argument("--out", default=None,
                   help="output JSONL path (default data/preference_pairs/<role>_synth.jsonl)")
    p.add_argument("--pairs-per-prompt", type=int, default=5,
                   help="how many (chosen, rejected) pairs to generate per prompt")
    p.add_argument("--max-prompts", type=int, default=None,
                   help="cap the corpus to the first N prompts (smoke test)")
    p.add_argument("--chosen-temp", type=float, default=0.0)
    p.add_argument("--rejected-temp", type=float, default=0.7)
    p.add_argument("--base-url", default=None)
    p.add_argument("--model", default=None)
    return p


def main() -> int:
    args = build_parser().parse_args()
    corpus_path = Path(args.corpus)
    if args.out:
        out_path = Path(args.out)
    else:
        out_path = Path("data/preference_pairs") / f"{args.role}_synth.jsonl"

    summary = asyncio.run(generate_pairs(
        corpus_path=corpus_path,
        out_path=out_path,
        pairs_per_prompt=args.pairs_per_prompt,
        base_url=args.base_url,
        model=args.model,
        chosen_temp=args.chosen_temp,
        rejected_temp=args.rejected_temp,
        max_prompts=args.max_prompts,
    ))
    print(json.dumps(summary, indent=2))
    return 0 if summary.get("ok") else 2


if __name__ == "__main__":
    raise SystemExit(main())
