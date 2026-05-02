"""
QuickCode V2 — SeekerDebugger (5-agent refinement pipeline).

Replaces the monolithic ``DebuggerAgent`` from ``code_intelligence``
with a 5-stage cooperative pipeline that keeps each stage simple
enough to debug independently:

    1. **Scanner**   — extracts the suspect region(s) from the
                       failing code + traceback.  Heuristic, no LLM.
    2. **Detector**  — classifies the failure mode (ImportError,
                       NameError, AssertionError, TimeoutError, …).
                       Heuristic on the stderr stream.
    3. **Predator**  — asks the LLM for K candidate patches.  Each
                       patch is a *full* replacement of the failing
                       code so the sandbox can run it directly.
    4. **Ranker**    — scores each candidate by a cheap heuristic
                       (closer to the original code = higher) and
                       (when ``mcts`` is provided) routes the
                       scoring through MCTS for an extra round of
                       evaluation.
    5. **Handler**   — applies the top-ranked patch, re-runs the
                       tier-aware sandbox, and returns the new
                       ``(code, tests, iterations)`` triple.

Failure mode
------------

* If any step throws or returns nothing, the previous code is
  returned unchanged with the iteration counter advanced — the
  pipeline never aborts the whole engine run.
* When the LLM is unavailable the Predator returns the original
  code as its sole candidate, so the engine keeps moving.

No content filters / refusal language anywhere.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any, Awaitable, Callable

from .contracts import SandboxResult, SandboxTier
from .mcts import MCTSRunner

logger = logging.getLogger(__name__)


LLMCall = Callable[[str, str | None, int], Awaitable[str]]
EventCallback = Callable[[str, dict[str, Any]], Awaitable[None]] | None


# ─────────────────────────────────────────────────────────────────────
# Prompts
#
# Each LLM-driven stage gets a tiny prompt.  No refusal language.
# ─────────────────────────────────────────────────────────────────────


PREDATOR_SYSTEM_PROMPT = """You are an expert software engineer fixing a failing test.

Given a function or module that crashed, propose 1 to 3 candidate
replacements.  Each candidate is a COMPLETE replacement of the
failing code (not a diff).  Match the original signature.  Direct,
technical, no apologies, no commentary outside the JSON.

Return STRICT JSON, no prose, no markdown fences:

{
  "candidates": [
    {"label": "fix_a", "code": "<full replacement>", "rationale": "<<=140 chars>"},
    {"label": "fix_b", "code": "<full replacement>", "rationale": "<<=140 chars>"}
  ]
}
"""


SCANNER_TRACE_RE = re.compile(r"^\s*File\s+\".*?\",\s+line\s+(\d+)", re.M)
SCANNER_NAME_RE = re.compile(r"NameError:\s+name\s+'([^']+)'\s+is\s+not\s+defined")
SCANNER_ATTR_RE = re.compile(r"AttributeError:\s+'[^']+'\s+object\s+has\s+no\s+attribute\s+'([^']+)'")


# ─────────────────────────────────────────────────────────────────────
# Failure classification (Detector)
# ─────────────────────────────────────────────────────────────────────


_DETECTOR_RULES: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"ImportError|ModuleNotFoundError"), "import_error"),
    (re.compile(r"NameError"), "name_error"),
    (re.compile(r"AttributeError"), "attribute_error"),
    (re.compile(r"TypeError"), "type_error"),
    (re.compile(r"ValueError"), "value_error"),
    (re.compile(r"AssertionError"), "assertion_error"),
    (re.compile(r"IndexError"), "index_error"),
    (re.compile(r"KeyError"), "key_error"),
    (re.compile(r"ZeroDivisionError"), "zero_division"),
    (re.compile(r"RecursionError"), "recursion_error"),
    (re.compile(r"SyntaxError"), "syntax_error"),
    (re.compile(r"timed?[\s_-]*out", re.I), "timeout"),
    (re.compile(r"MemoryError"), "memory_error"),
)


def classify_failure(stderr: str) -> str:
    """Return a short slug for the first matching failure rule.

    Falls back to ``"unknown"``."""
    if not stderr:
        return "unknown"
    for pat, slug in _DETECTOR_RULES:
        if pat.search(stderr):
            return slug
    return "unknown"


# ─────────────────────────────────────────────────────────────────────
# Scanner
# ─────────────────────────────────────────────────────────────────────


def scan_failure(
    code: str,
    failure: SandboxResult,
    *,
    context_lines: int = 4,
) -> dict[str, Any]:
    """Pull the most-likely suspect lines + missing names from the
    code + stderr.  Pure, deterministic, fast."""
    lines = (code or "").splitlines()
    suspects: list[tuple[int, str]] = []
    seen: set[int] = set()
    for m in SCANNER_TRACE_RE.finditer(failure.stderr or ""):
        try:
            ln = int(m.group(1))
        except ValueError:
            continue
        for offset in range(-context_lines, context_lines + 1):
            ln_target = ln + offset
            if 1 <= ln_target <= len(lines) and ln_target not in seen:
                seen.add(ln_target)
                suspects.append((ln_target, lines[ln_target - 1]))

    missing_names = SCANNER_NAME_RE.findall(failure.stderr or "")
    missing_attrs = SCANNER_ATTR_RE.findall(failure.stderr or "")

    return {
        "suspects": suspects[:50],
        "missing_names": missing_names[:10],
        "missing_attrs": missing_attrs[:10],
    }


# ─────────────────────────────────────────────────────────────────────
# Public class
# ─────────────────────────────────────────────────────────────────────


class SeekerDebugger:
    """5-agent refinement pipeline."""

    PREDATOR_SYSTEM_PROMPT = PREDATOR_SYSTEM_PROMPT

    def __init__(
        self,
        *,
        llm_call: LLMCall | None = None,
        sandbox: Any | None = None,
        on_event: EventCallback = None,
        max_iters: int = 3,
        max_tokens: int = 1500,
        mcts: MCTSRunner | None = None,
        tier: SandboxTier = SandboxTier.QUICK,
    ) -> None:
        self._llm_call = llm_call
        self._sandbox = sandbox
        self._on_event = on_event
        self._max_iters = max(0, min(3, int(max_iters)))
        self._max_tokens = max(256, int(max_tokens))
        self._mcts = mcts
        self._tier = tier

    @property
    def max_iters(self) -> int:
        return self._max_iters

    # ─── Public API ─────────────────────────────────────────────────

    async def refine(
        self,
        code: str,
        tests: str | None,
        last_failure: SandboxResult,
        *,
        language: str = "python",
    ) -> tuple[str, str | None, int]:
        """Iterate up to ``max_iters`` Scanner → Predator → Handler
        rounds.  Returns ``(refined_code, refined_tests, iters_used)``.

        Tests are passed through unchanged today — the Seeker focuses
        on code patches.  A future iteration could add a TestSeeker
        that mirrors this pipeline for the test file."""
        if self._max_iters == 0 or not code:
            return code, tests, 0

        current_code = code
        current_failure = last_failure
        iters_used = 0

        for i in range(self._max_iters):
            iters_used = i + 1
            scan = scan_failure(current_code, current_failure)
            failure_class = classify_failure(current_failure.stderr or "")
            await self._emit("seeker_scan", {
                "iteration": iters_used,
                "failure_class": failure_class,
                "suspects": [
                    {"line": ln, "src": src} for ln, src in scan["suspects"][:5]
                ],
                "missing_names": scan["missing_names"],
                "missing_attrs": scan["missing_attrs"],
            })

            candidates = await self._predate(
                code=current_code,
                tests=tests,
                scan=scan,
                failure_class=failure_class,
                language=language,
            )
            if not candidates:
                await self._emit("seeker_no_candidates", {"iteration": iters_used})
                break

            ranked = await self._rank(current_code, candidates)
            patch = ranked[0]
            new_result = await self._run_sandbox(patch, language=language)
            await self._emit("seeker_handler", {
                "iteration": iters_used,
                "ok": bool(new_result.ok),
                "exit_code": new_result.exit_code,
            })

            if new_result.ok:
                return patch, tests, iters_used

            current_code = patch
            current_failure = new_result

        return current_code, tests, iters_used

    # ─── Stage 3: Predator ──────────────────────────────────────────

    async def _predate(
        self,
        *,
        code: str,
        tests: str | None,
        scan: dict[str, Any],
        failure_class: str,
        language: str,
    ) -> list[str]:
        prompt = self._predator_prompt(
            code=code,
            tests=tests,
            scan=scan,
            failure_class=failure_class,
            language=language,
        )
        try:
            llm = await self._ensure_llm()
            raw = await llm(prompt, self.PREDATOR_SYSTEM_PROMPT, self._max_tokens)
        except Exception as exc:  # pragma: no cover - infra
            logger.debug("seeker predator LLM failed: %s", exc)
            return [code]

        parsed = self._parse_candidates(raw or "")
        if not parsed:
            return [code]
        return parsed

    def _predator_prompt(
        self,
        *,
        code: str,
        tests: str | None,
        scan: dict[str, Any],
        failure_class: str,
        language: str,
    ) -> str:
        rows: list[str] = [
            f"# Failure class: {failure_class}",
            f"# Language: {language}",
        ]
        if scan["missing_names"]:
            rows.append("# Missing names: " + ", ".join(scan["missing_names"][:6]))
        if scan["missing_attrs"]:
            rows.append("# Missing attributes: " + ", ".join(scan["missing_attrs"][:6]))
        if scan["suspects"]:
            rows.append("# Suspect lines:")
            for ln, src in scan["suspects"][:8]:
                rows.append(f"  {ln:4d} | {src}")
        rows.append("\n# Failing code")
        rows.append("```" + language)
        rows.append(code[:8000])
        rows.append("```")
        if tests:
            rows.append("\n# Tests (do NOT modify)")
            rows.append("```" + language)
            rows.append(tests[:4000])
            rows.append("```")
        rows.append(
            "\nReturn the JSON described in the system prompt. No prose."
        )
        return "\n".join(rows)

    @staticmethod
    def _parse_candidates(text: str) -> list[str]:
        text = (text or "").strip()
        if not text:
            return []
        # Strip a single fenced block if present.
        m = re.search(r"```(?:json)?\s*(.*?)\s*```", text, re.S | re.I)
        blob = m.group(1) if m else text
        try:
            data = json.loads(blob)
        except json.JSONDecodeError:
            return []
        if not isinstance(data, dict):
            return []
        candidates_raw = data.get("candidates") or []
        out: list[str] = []
        for raw in candidates_raw:
            if not isinstance(raw, dict):
                continue
            code = str(raw.get("code") or "").strip()
            if code:
                out.append(code)
        return out[:3]

    # ─── Stage 4: Ranker ────────────────────────────────────────────

    async def _rank(self, original: str, candidates: list[str]) -> list[str]:
        if not candidates:
            return []
        if self._mcts is None or len(candidates) == 1:
            scored = [
                (self._heuristic_score(original, c), c) for c in candidates
            ]
            scored.sort(key=lambda x: x[0], reverse=True)
            return [c for _, c in scored]

        # MCTS path: build CodeSnippet wrappers and use the heuristic
        # score as a synchronous oracle.
        from .contracts import CodeSnippet

        snippets = [
            CodeSnippet(source=c, score=0.0, language="python")
            for c in candidates
        ]

        def _scorer(s: CodeSnippet) -> float:
            return self._heuristic_score(original, s.source)

        winner, _ = await self._mcts.select(snippets, _scorer)
        # Move the winner to the front, preserve the rest in heuristic order.
        rest = [c for c in candidates if c != winner.source]
        rest.sort(key=lambda c: self._heuristic_score(original, c), reverse=True)
        return [winner.source, *rest]

    @staticmethod
    def _heuristic_score(original: str, candidate: str) -> float:
        """Rough proximity score in [0, 1].  Higher = closer to the
        original.  Penalises candidates that throw away most of the
        file or balloon to 4× the size."""
        if not original or not candidate:
            return 0.0
        ratio = len(candidate) / max(1, len(original))
        if ratio < 0.5 or ratio > 4.0:
            return 0.1
        # Token overlap
        a = set(re.findall(r"[A-Za-z_]\w*", original))
        b = set(re.findall(r"[A-Za-z_]\w*", candidate))
        if not a:
            return 0.0
        overlap = len(a & b) / len(a)
        return round(0.3 * (1.0 - abs(1.0 - ratio) / 3.0) + 0.7 * overlap, 4)

    # ─── Stage 5: Handler ──────────────────────────────────────────

    async def _run_sandbox(
        self, code: str, *, language: str
    ) -> SandboxResult:
        if self._sandbox is None:
            return SandboxResult(ok=False, stderr="no sandbox configured", tier=self._tier)
        try:
            res = await self._sandbox.execute(
                code, language=language, tier=self._tier
            )
        except Exception as exc:  # pragma: no cover - infra
            return SandboxResult(
                ok=False,
                stderr=f"{type(exc).__name__}: {exc}"[:8000],
                tier=self._tier,
            )
        if isinstance(res, SandboxResult):
            return res
        return SandboxResult.from_dict(res if isinstance(res, dict) else None)

    # ─── Internals ──────────────────────────────────────────────────

    async def _ensure_llm(self) -> LLMCall:
        if self._llm_call is None:
            from ..api.code_intelligence_routes import _llm_call_local  # noqa: PLC0415

            self._llm_call = _llm_call_local
        return self._llm_call

    async def _emit(self, event: str, payload: dict[str, Any]) -> None:
        if self._on_event is None:
            return
        try:
            await self._on_event(event, payload)
        except Exception as exc:  # pragma: no cover
            logger.debug("seeker on_event(%s) failed: %s", event, exc)


__all__ = [
    "SeekerDebugger",
    "PREDATOR_SYSTEM_PROMPT",
    "scan_failure",
    "classify_failure",
]
