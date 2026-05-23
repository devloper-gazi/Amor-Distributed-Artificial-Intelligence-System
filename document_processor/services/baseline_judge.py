"""
Sprint 0 critic-as-judge layer.

Talks to a CPU-only ``llama-server`` running Mistral-Small-3-24B-Instruct
(Q4_K_M, ~14 GB on disk) and grades each Sprint 0 baseline row on a
2-rubric scale (correctness 1-5 + completeness 1-5).  Applies
position-swap dedup: judge each pair A/B then B/A; if disagreement on
either rubric is >1 point, mark the row "uncertain".

Why a separate family from the candidates:  AMOR's stack judges
Qwen-derived outputs (qwen2.5-coder, qwen3, deepseek-r1-qwen3-distill).
A Qwen-family judge would self-prefer (Panickssery 2024, Liu 2024).
Mistral-Small-3 is Apache-2.0 and lineage-distinct; Phi-4-14B (MIT) is
the lighter fallback if the 24B is too slow on the host.

CPU latency on 8-core / 32 GB:  ~60-90 s per query (24B Q4_K_M).
A full 10-prompt corpus × 2 rubrics × 2 positions = 40 queries ≈
40-60 min total — acceptable for a 3-day Sprint 0 exercise that runs
once or twice a week.

The judge is OPT-IN — Day 1 ships ``--no-judge`` mode that skips this
layer entirely so the runner is usable before the GGUF lands on disk.

Service start (manual, before running the baseline)::

    docker run -d --rm --name amor-judge \\
        -v ./data/custom_models/judge:/models:ro \\
        -p 127.0.0.1:9101:8080 \\
        ghcr.io/ggml-org/llama.cpp:server \\
        -m /models/Mistral-Small-24B-Instruct-2501-Q4_K_M.gguf \\
        --host 0.0.0.0 --port 8080 \\
        --ctx-size 4096 --threads 8 --batch-size 256

or via the helper at ``tools/judge/start_judge.sh``.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

import httpx

logger = logging.getLogger(__name__)


# ─── prompt template ────────────────────────────────────────────────


_JUDGE_SYSTEM = (
    "You are a senior engineer evaluating an AI assistant's response to "
    "a developer prompt.  You apply two independent rubrics (correctness "
    "and completeness), each on a 1-5 integer scale.  You are concise, "
    "honest, and never inflate scores out of politeness.  You output "
    "STRICTLY one JSON object — nothing before, nothing after, no "
    "markdown fences."
)

_JUDGE_USER_TEMPLATE = """\
Original prompt
---------------
{prompt}

Response A
----------
{response_a}

Response B
----------
{response_b}

Rubric instructions
-------------------
Score each response on TWO rubrics, each 1-5 (integers only):

* correctness:  1=wrong/broken, 2=major errors, 3=mostly right with
  notable gaps, 4=correct with minor issues, 5=fully correct.
* completeness: 1=trivial/missing, 2=major omissions, 3=adequate but
  shallow, 4=thorough, 5=exhaustive and well-structured.

Output format (STRICT — no surrounding text, no fences):

{{"a": {{"correctness": <1-5>, "completeness": <1-5>}},
 "b": {{"correctness": <1-5>, "completeness": <1-5>}},
 "rationale": "<one sentence; <=80 chars>"}}
"""


# ─── dataclasses ────────────────────────────────────────────────────


@dataclass(frozen=True)
class JudgeConfig:
    base_url: str = "http://localhost:9101"
    model: str = "mistral-small-3-q4km"
    request_timeout_s: float = 600.0   # bump 240→600 — CPU 24B prompt-eval
                                        # phase alone hits ~120s on a
                                        # 1500-token prompt; 240 was the
                                        # source of 7/9 cancel-task errors
                                        # in the first Sprint 0 run.
    temperature: float = 0.0           # deterministic
    max_tokens: int = 160              # we only want the JSON object;
                                        # 256 was wasteful for our schema.
    health_path: str = "/health"


@dataclass
class RubricScore:
    correctness: int  # 1..5
    completeness: int  # 1..5

    def to_dict(self) -> Dict[str, int]:
        return {"correctness": self.correctness, "completeness": self.completeness}


@dataclass
class JudgeResult:
    score: Optional[RubricScore]
    uncertain: bool
    rationale: str
    raw_a_to_b: Optional[Dict[str, Any]] = None
    raw_b_to_a: Optional[Dict[str, Any]] = None
    error: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        out: Dict[str, Any] = {
            "uncertain": self.uncertain,
            "rationale": self.rationale,
        }
        if self.score is not None:
            out["correctness"] = self.score.correctness
            out["completeness"] = self.score.completeness
        if self.error:
            out["error"] = self.error
        return out


# ─── HTTP client + parser ───────────────────────────────────────────


async def _post_chat(
    client: httpx.AsyncClient,
    cfg: JudgeConfig,
    *,
    user_text: str,
) -> Dict[str, Any]:
    """Single OpenAI-compat completion call to the judge llama-server."""
    payload = {
        "model": cfg.model,
        "messages": [
            {"role": "system", "content": _JUDGE_SYSTEM},
            {"role": "user", "content": user_text},
        ],
        "temperature": cfg.temperature,
        "max_tokens": cfg.max_tokens,
        "stream": False,
    }
    url = cfg.base_url.rstrip("/") + "/v1/chat/completions"
    response = await client.post(url, json=payload, timeout=cfg.request_timeout_s)
    response.raise_for_status()
    return response.json()


_JSON_OBJECT_RE = re.compile(r"\{.*\}", re.DOTALL)


def _extract_json(blob: str) -> Optional[Dict[str, Any]]:
    """Best-effort extract a single JSON object from a model reply.
    Tolerates leading/trailing prose, fenced code blocks, etc."""
    if not blob:
        return None
    candidate = blob.strip()
    # If the whole thing parses, take it.
    try:
        parsed = json.loads(candidate)
        if isinstance(parsed, dict):
            return parsed
    except json.JSONDecodeError:
        pass
    # Otherwise, regex-grab the largest {…} chunk.
    match = _JSON_OBJECT_RE.search(blob)
    if not match:
        return None
    try:
        parsed = json.loads(match.group(0))
        if isinstance(parsed, dict):
            return parsed
    except json.JSONDecodeError:
        # Strip trailing commas — common LLM-output bug.
        cleaned = re.sub(r",(\s*[}\]])", r"\1", match.group(0))
        try:
            parsed = json.loads(cleaned)
            if isinstance(parsed, dict):
                return parsed
        except json.JSONDecodeError:
            return None
    return None


def _coerce_score(raw: Any) -> Optional[RubricScore]:
    if not isinstance(raw, dict):
        return None
    try:
        c = int(raw.get("correctness", 0))
        m = int(raw.get("completeness", 0))
    except (TypeError, ValueError):
        return None
    if not (1 <= c <= 5 and 1 <= m <= 5):
        return None
    return RubricScore(correctness=c, completeness=m)


# ─── public: judge a single row ─────────────────────────────────────


async def judge_pair(
    *,
    prompt: str,
    candidate_a: str,
    candidate_b: str,
    cfg: JudgeConfig,
    client: httpx.AsyncClient,
) -> Tuple[Optional[RubricScore], Optional[RubricScore], str, Dict[str, Any]]:
    """Run ONE A/B comparison.  Returns (score_a, score_b, rationale, raw)."""
    # Cap each block at 3000 chars (~750 tokens) to keep total prompt
    # under ~2.5K tokens.  CPU 24B prompt-eval scales linearly with
    # input size; the empirical cliff is ~3000 tokens beyond which
    # judges miss the 5-min client timeout window.
    user_text = _JUDGE_USER_TEMPLATE.format(
        prompt=prompt[:3000],
        response_a=candidate_a[:3000],
        response_b=candidate_b[:3000],
    )
    raw = await _post_chat(client, cfg, user_text=user_text)
    text = ""
    try:
        text = raw["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError):
        text = ""
    parsed = _extract_json(text)
    if not parsed:
        return None, None, "judge returned no parseable JSON", raw
    score_a = _coerce_score(parsed.get("a"))
    score_b = _coerce_score(parsed.get("b"))
    rationale = str(parsed.get("rationale") or "")[:120]
    return score_a, score_b, rationale, raw


async def judge_with_swap(
    *,
    prompt: str,
    candidate: str,
    reference: str,
    cfg: JudgeConfig,
    client: httpx.AsyncClient,
) -> JudgeResult:
    """Position-swap dedup: judge (candidate=A, reference=B) AND
    (reference=A, candidate=B).  If the candidate's per-rubric score
    diverges by >1 point across the two positions, mark uncertain.

    The "score" we keep is the candidate's mean across the two passes
    (rounded to nearest integer).  Reference scores are recorded but
    not surfaced upstream.
    """
    # Pass 1: candidate=A, reference=B
    a1, b1, rationale1, raw1 = await judge_pair(
        prompt=prompt, candidate_a=candidate, candidate_b=reference,
        cfg=cfg, client=client,
    )
    # Pass 2: position-swap — candidate=B, reference=A
    a2, b2, rationale2, raw2 = await judge_pair(
        prompt=prompt, candidate_a=reference, candidate_b=candidate,
        cfg=cfg, client=client,
    )

    if a1 is None or b1 is None:
        return JudgeResult(
            score=None, uncertain=True,
            rationale=f"pass-1 unparseable: {rationale1}",
            raw_a_to_b=raw1, raw_b_to_a=raw2,
            error="pass-1 score missing",
        )
    if a2 is None or b2 is None:
        return JudgeResult(
            score=None, uncertain=True,
            rationale=f"pass-2 unparseable: {rationale2}",
            raw_a_to_b=raw1, raw_b_to_a=raw2,
            error="pass-2 score missing",
        )

    # Candidate's two readings: pass-1 a1, pass-2 b2 (it was 'B' in pass-2).
    cand_pass1 = a1
    cand_pass2 = b2

    # Disagreement check.
    delta_corr = abs(cand_pass1.correctness - cand_pass2.correctness)
    delta_comp = abs(cand_pass1.completeness - cand_pass2.completeness)
    uncertain = delta_corr > 1 or delta_comp > 1

    # Mean — clamp to [1,5].  Use half-up rounding (NOT Python's
    # default banker's rounding) so 4.5 → 5 consistently; users find
    # banker's rounding (4.5 → 4) unintuitive when reading a single
    # judge score off the dashboard.
    def _half_up(x: float) -> int:
        return int(x + 0.5) if x >= 0 else -int(-x + 0.5)

    mean_corr = max(1, min(5, _half_up((cand_pass1.correctness + cand_pass2.correctness) / 2)))
    mean_comp = max(1, min(5, _half_up((cand_pass1.completeness + cand_pass2.completeness) / 2)))

    rationale = (rationale1 or rationale2)[:300]
    return JudgeResult(
        score=RubricScore(correctness=mean_corr, completeness=mean_comp),
        uncertain=uncertain,
        rationale=rationale,
        raw_a_to_b=raw1, raw_b_to_a=raw2,
    )


# ─── public: health ─────────────────────────────────────────────────


async def is_judge_healthy(cfg: JudgeConfig) -> bool:
    """Return True iff the judge llama-server responds 200 on /health
    within 5 s.  Never raises."""
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            r = await client.get(cfg.base_url.rstrip("/") + cfg.health_path)
            return r.status_code == 200
    except Exception:
        return False


# ─── public: batch (used by run_sprint0_baseline) ───────────────────


@dataclass
class JudgeBatchInput:
    """One slot in the batch — judge ``candidate`` against an empty
    reference for absolute scoring (no comparator).  When we have a
    natural reference (e.g. golden output for build-fizzbuzz), pass it
    in via ``reference``.  When we don't, the empty string acts as a
    "minimum baseline" — the judge anchors scores on the prompt alone."""

    prompt_id: str
    prompt: str
    candidate: str
    reference: str = ""


async def judge_batch(
    inputs: List[JudgeBatchInput],
    *,
    cfg: JudgeConfig,
    concurrency: int = 1,  # CPU-only judge; serial is fine
) -> Dict[str, JudgeResult]:
    """Run the judge over a list of inputs.  Returns
    ``{prompt_id: JudgeResult}``.  Failures per-row are encoded into
    the JudgeResult.error field — never raises out of a row's failure."""
    results: Dict[str, JudgeResult] = {}
    semaphore = asyncio.Semaphore(max(1, concurrency))

    async with httpx.AsyncClient(timeout=cfg.request_timeout_s) as client:
        async def _one(item: JudgeBatchInput) -> None:
            async with semaphore:
                try:
                    res = await judge_with_swap(
                        prompt=item.prompt,
                        candidate=item.candidate,
                        reference=item.reference,
                        cfg=cfg, client=client,
                    )
                except httpx.HTTPError as exc:
                    res = JudgeResult(
                        score=None, uncertain=True,
                        rationale="", error=f"http error: {exc}",
                    )
                except Exception as exc:  # pragma: no cover
                    logger.exception("judge crashed for %s", item.prompt_id)
                    res = JudgeResult(
                        score=None, uncertain=True,
                        rationale="", error=f"crash: {exc}",
                    )
                results[item.prompt_id] = res

        await asyncio.gather(*[_one(it) for it in inputs])

    return results


__all__ = [
    "JudgeConfig",
    "JudgeBatchInput",
    "JudgeResult",
    "RubricScore",
    "is_judge_healthy",
    "judge_batch",
    "judge_pair",
    "judge_with_swap",
]
