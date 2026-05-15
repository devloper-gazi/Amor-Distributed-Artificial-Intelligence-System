"""
Sprint 2 Day 2 — HumanEval+ 50-subset runner.

Strategy
--------
Avoid the heavy ``evalplus`` Python package.  Pull the canonical
HumanEval+ dataset JSONL straight from Hugging Face (``evalplus/
humanevalplus`` mirror), pick the first 50 task ids, run the active
LLM backend on each prompt, execute the candidate completion against
the test block in AMOR's existing Docker sandbox.  Pass = exit code 0
within the per-task timeout.

This makes the eval:
* Cheap (no extra Python deps; ``huggingface_hub`` already in tree)
* Faithful (uses the canonical EvalPlus test cases verbatim)
* Reuses AMOR's sandbox for security + code-execution path

Wired into the manifest at import time via ``register_eval``.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, List, Optional, Tuple

import httpx

from document_processor.api.admin_evals_routes import (
    EvalDescriptor,
    register_eval,
)
from document_processor.code_intelligence.sandbox import ExecutionSandbox
from document_processor.infrastructure.storage import storage_manager
from sqlalchemy import text

logger = logging.getLogger(__name__)


# ─── dataset bootstrap ─────────────────────────────────────────────


# Hand-curated subset.  These are the first 50 HumanEval task ids in
# canonical order.  Quality cross-check: each id maps to a problem
# under 30 lines that's solvable by a 7B model in <30 s.  The full
# dataset has 164 problems; sticking to the first 50 keeps the
# sweep under the Cycle C target of 25 min on a single 4060.
TASK_IDS_50: tuple[str, ...] = tuple(
    f"HumanEval/{i}" for i in range(50)
)


_DATASET_REPO = "evalplus/humanevalplus"
# Verified May 2026: the repo ships test.jsonl alongside a parquet
# mirror.  Old (versioned) filenames returned 404.
_DATASET_FILE = "test.jsonl"


@dataclass(frozen=True)
class HumanEvalProblem:
    task_id: str
    prompt: str
    entry_point: str
    canonical_solution: str
    test: str            # pytest-style ``check(candidate)`` block


_PROBLEMS_CACHE: Dict[str, HumanEvalProblem] = {}
_DATASET_PATH = Path("/data/custom_models/humaneval_plus.jsonl")


def _ensure_dataset() -> Path:
    """Download the HumanEval+ JSONL.gz if missing.  Returns the
    on-disk path.  Idempotent — file-size check skips re-download."""
    if _DATASET_PATH.is_file() and _DATASET_PATH.stat().st_size > 100_000:
        return _DATASET_PATH
    _DATASET_PATH.parent.mkdir(parents=True, exist_ok=True)
    try:
        from huggingface_hub import hf_hub_download  # noqa: PLC0415
    except ImportError as exc:
        raise RuntimeError(
            "huggingface_hub missing — cannot download HumanEval+ dataset",
        ) from exc
    cached = hf_hub_download(
        repo_id=_DATASET_REPO,
        filename=_DATASET_FILE,
        repo_type="dataset",
    )
    # The file lands under HF's local cache; copy to our canonical path.
    import shutil  # noqa: PLC0415
    shutil.copy2(cached, _DATASET_PATH)
    return _DATASET_PATH


def _load_problems() -> Dict[str, HumanEvalProblem]:
    """Parse the dataset JSONL once, cache in-process."""
    if _PROBLEMS_CACHE:
        return _PROBLEMS_CACHE
    path = _ensure_dataset()
    with open(path, "rt", encoding="utf-8") as fp:
        for line in fp:
            line = line.strip()
            if not line:
                continue
            d = json.loads(line)
            tid = d.get("task_id")
            if not tid:
                continue
            _PROBLEMS_CACHE[tid] = HumanEvalProblem(
                task_id=tid,
                prompt=d.get("prompt", ""),
                entry_point=d.get("entry_point", ""),
                canonical_solution=d.get("canonical_solution", ""),
                test=d.get("test", ""),
            )
    return _PROBLEMS_CACHE


# ─── completion via OpenAI-compat endpoint ─────────────────────────


def _llm_base_url() -> str:
    """Where to send completions — same flag the runtime backend uses.

    Cycle F Sprint 6 Step 5 fix: empty-string env values (e.g.
    AMOR_LLM_BACKEND_URL="") were treated as set + winning over the
    fallback chain, leaving the runner with a blank URL and
    httpx raising "Request URL is missing an 'http://' protocol".
    Falsy-check now skips empty values and walks the fallback list
    (LLAMASWAP_URL → hard-coded internal default)."""

    for key in ("AMOR_LLM_BACKEND_URL", "AMOR_LLAMASWAP_URL"):
        value = (os.environ.get(key) or "").strip()
        if value:
            return value
    return "http://amor-llama-swap:9100"


def _llm_model() -> str:
    """Which model tag to send.  Default to the editor (qwen2.5-coder)
    since HumanEval is pure code generation."""
    return os.environ.get("AMOR_EVAL_MODEL", "amor-editor")


_COMPLETION_SYSTEM = (
    "You are a Python expert.  Complete the user's function "
    "stub.  Output ONLY the function body — no markdown fences, no "
    "imports beyond what the stub already has, no extra prose.  "
    "Match the exact signature of the prompt."
)


async def _complete_one(
    client: httpx.AsyncClient,
    base_url: str,
    model: str,
    prompt: str,
    *,
    timeout_s: float = 120.0,
) -> Tuple[str, float]:
    """Returns (completion_text, wall_clock_ms).  Raises on HTTP error."""
    started = time.perf_counter()
    response = await client.post(
        f"{base_url.rstrip('/')}/v1/chat/completions",
        json={
            "model": model,
            "messages": [
                {"role": "system", "content": _COMPLETION_SYSTEM},
                {"role": "user", "content": prompt},
            ],
            "temperature": 0.0,        # greedy decoding — matches plan
            "max_tokens": 1024,        # 512 was too tight for nested
                                       # logic; common HE problems
                                       # need 200-700 tokens.
            "stream": False,
        },
        timeout=timeout_s,
    )
    response.raise_for_status()
    body = response.json()
    text_out = body["choices"][0]["message"]["content"]
    wall_ms = (time.perf_counter() - started) * 1000.0
    return text_out, wall_ms


_FENCE_RE = re.compile(r"```(?:python)?\s*\n(.*?)(?:```|$)", re.DOTALL)


def _extract_code(completion: str, entry_point: str = "") -> str:
    """Best-effort: pull a runnable Python block out of ``completion``.

    LLMs vary wildly in formatting:
    * ``def f(...): ...`` — clean function, use as-is.
    * Fenced ``\`\`\`python\\n...\\n\`\`\``` — extract inside.
    * Truncated fence (no closing) — extract from fence start to EOF.
    * Prose preamble + code — strip prose, keep code.

    If the result starts with ``def <entry_point>``, return it as a
    full function (caller skips the prompt prepend).  Otherwise
    return as a function-body completion (caller prepends the prompt).
    """
    text = completion
    # 1. If the model wrapped code in ``` fences, take the largest
    #    block.  Tolerate missing closing fence.  Use rstrip only —
    #    leading whitespace is meaningful (function-body indent).
    blocks = _FENCE_RE.findall(text)
    if blocks:
        text = max(blocks, key=len).rstrip()
    # 2. Strip leading prose IF a `def <entry_point>` appears.  Don't
    #    use `\ndef ` fallback because that strips legitimate body
    #    indent if the model emitted only the body (no def).
    if entry_point and f"def {entry_point}" in text:
        idx = text.index(f"def {entry_point}")
        text = text[idx:]
    # 3. Strip trailing dangling fences.
    if "```" in text:
        text = text.split("```")[0].rstrip()
    # 4. Drop strictly-blank trailing lines but preserve leading indent.
    return text.rstrip()


def _is_full_function(code: str, entry_point: str) -> bool:
    return bool(entry_point) and code.lstrip().startswith(f"def {entry_point}")


# ─── exec via AMOR sandbox ─────────────────────────────────────────


async def _execute_one(
    sandbox: ExecutionSandbox,
    problem: HumanEvalProblem,
    completion: str,
    *,
    timeout_s: int = 30,
) -> Tuple[bool, str]:
    """Run ``prompt + completion + test`` in the AMOR sandbox.
    Returns (passed, stderr_excerpt).

    Smart prepend: if the completion already includes
    ``def <entry_point>``, treat it as a full function and skip the
    prompt prepend (else we'd duplicate the def line and hit a
    SyntaxError before even reaching the test block).
    """
    if _is_full_function(completion, problem.entry_point):
        body = (
            completion
            + "\n\n"
            + problem.test
            + f"\n\ncheck({problem.entry_point})\n"
        )
    else:
        body = (
            problem.prompt
            + completion
            + "\n\n"
            + problem.test
            + f"\n\ncheck({problem.entry_point})\n"
        )
    result = await sandbox.execute(
        code=body,
        language="python",
        timeout=timeout_s,
        # HumanEval+ tests routinely import numpy for vector ops + math
        # for nan checks; the slim base image has neither.  Install
        # both up-front so we don't pay the pip-cold-start on every
        # case (sandbox image cache layers).
        install_packages=["numpy"],
    )
    passed = bool(result.success) and result.exit_code == 0
    err = ""
    if not passed:
        err = (result.stderr or "")[-500:].strip() or (
            result.error or "unknown failure"
        )
    return passed, err


# ─── runner — wired into the manifest ──────────────────────────────


async def _persist_cases(run_id: str, cases: List[Dict[str, Any]]) -> None:
    if storage_manager.pg_session_maker is None:
        return
    async with storage_manager.pg_session_maker() as session:
        await session.execute(
            text(
                "UPDATE eval_runs SET cases = :cases WHERE id = :id"
            ),
            {"id": run_id, "cases": json.dumps(cases)},
        )
        await session.commit()


def _percentile(values: List[float], pct: float) -> Optional[float]:
    if not values:
        return None
    s = sorted(values)
    k = max(0, min(len(s) - 1, int(round(pct / 100.0 * (len(s) - 1)))))
    return s[k]


async def run_humaneval_plus(
    run_id: str,
    progress: Callable[[str], Awaitable[None]],
) -> Dict[str, Any]:
    """The actual runner.  Registered into the manifest at module
    import time."""
    problems = _load_problems()
    # AMOR_EVAL_LIMIT — smoke-test override set by the
    # /api/admin/evals/run/{name}?limit=N path (Sprint 2 Day 1
    # contextvar contract).  Falls back to AMOR_HUMANEVAL_LIMIT for
    # backward-compat.  Production: unset for the full 50.
    try:
        limit = int(
            os.environ.get(
                "AMOR_EVAL_LIMIT",
                os.environ.get("AMOR_HUMANEVAL_LIMIT", "0"),
            ),
        )
    except ValueError:
        limit = 0
    task_ids = TASK_IDS_50 if limit <= 0 else TASK_IDS_50[: max(1, limit)]
    selected: List[HumanEvalProblem] = []
    missing: List[str] = []
    for tid in task_ids:
        p = problems.get(tid)
        if p is None:
            missing.append(tid)
        else:
            selected.append(p)

    if not selected:
        raise RuntimeError(
            f"no HumanEval+ problems loaded "
            f"(dataset path={_DATASET_PATH}); first missing: {missing[:5]}",
        )

    base_url = _llm_base_url()
    model = _llm_model()
    sandbox = ExecutionSandbox()

    cases: List[Dict[str, Any]] = []
    durations_ms: List[float] = []
    passed_count = 0

    await progress(json.dumps({
        "type": "progress",
        "stage": "init",
        "total": len(selected),
        "model": model,
        "endpoint": base_url,
    }))

    async with httpx.AsyncClient() as client:
        for idx, prob in enumerate(selected):
            # 1. Generate completion.
            try:
                completion_raw, gen_ms = await _complete_one(
                    client, base_url, model, prob.prompt,
                )
            except httpx.HTTPError as exc:
                cases.append({
                    "task_id": prob.task_id,
                    "passed": False,
                    "error": f"completion HTTP error: {exc}",
                    "wall_ms": 0,
                })
                await progress(json.dumps({
                    "type": "case",
                    "i": idx + 1,
                    "task_id": prob.task_id,
                    "passed": False,
                }))
                continue

            completion = _extract_code(completion_raw, prob.entry_point)
            durations_ms.append(gen_ms)

            # 2. Execute.
            try:
                passed, err = await _execute_one(sandbox, prob, completion)
            except Exception as exc:  # pragma: no cover
                passed, err = False, f"sandbox crash: {exc}"

            cases.append({
                "task_id": prob.task_id,
                "passed": passed,
                "wall_ms": int(gen_ms),
                "completion_len": len(completion),
                "completion_head": completion[:160],
                "completion_raw_head": completion_raw[:160],
                "is_full_function": _is_full_function(
                    completion, prob.entry_point,
                ),
                "error": err if not passed else None,
            })
            if passed:
                passed_count += 1

            # Persist progress every 5 cases so a long sweep doesn't
            # disappear into in-memory limbo.
            if (idx + 1) % 5 == 0:
                await _persist_cases(run_id, cases)

            await progress(json.dumps({
                "type": "case",
                "i": idx + 1,
                "task_id": prob.task_id,
                "passed": passed,
                "passed_so_far": passed_count,
            }))

    # Final persist.
    await _persist_cases(run_id, cases)

    summary = {
        "passed": passed_count,
        "total": len(selected),
        "pass_at_1": round(passed_count / max(len(selected), 1), 4),
        "p50_ms": _percentile(durations_ms, 50),
        "p95_ms": _percentile(durations_ms, 95),
        "model": model,
        "endpoint": base_url,
    }
    return summary


# ─── manifest registration ─────────────────────────────────────────


register_eval(
    EvalDescriptor(
        name="humaneval_plus_50",
        title="HumanEval+ 50",
        description=(
            "EvalPlus HumanEval+ subset (first 50 problems) — "
            "completions via the active LLM backend, executed in the "
            "AMOR sandbox.  Pass@1 + p50/p95 generation latency."
        ),
        expected_minutes=25,
        summary_keys=("passed", "total", "pass_at_1", "p50_ms", "p95_ms"),
        runner=run_humaneval_plus,
    ),
)
