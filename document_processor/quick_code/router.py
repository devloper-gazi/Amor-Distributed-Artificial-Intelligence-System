"""
QuickCode V2 — TaskClassifier (hybrid heuristic + 1.5B LLM router).

The router answers a single question: which complexity bucket does
this prompt belong to?

    TRIVIAL  →  one-line / one-function tasks; minimal scaffolding
                (e.g. "reverse a string", "sum a list").
    SIMPLE   →  small standalone solutions, ≤ ~100 LOC, ≤ 3 helpers
                (e.g. "implement a stable merge sort").
    COMPLEX  →  multi-file / multi-component work that benefits from
                the Pro Code Intelligence engine; we auto-redirect
                the user there when ``mode='quick'``.
    MATH     →  symbolic / numerical / proof-heavy tasks. Triggers
                ``symcode.SymCode`` validation downstream.

Design
------

* **Heuristic first.** A regex sweep over the prompt is enough to
  classify the obvious cases for free.  Saves an LLM call on roughly
  ~70 % of prompts on the existing benchmark suite.
* **LLM fallback.** When the heuristic is uncertain
  (i.e. ambiguous keyword profile) we hand the prompt to a small
  1.5 B model (default ``qwen2.5:1.5b``) for a single-token answer.
* **Fail-soft.** Every external call (LLM, settings) is wrapped in
  try/except so that a transient infra failure never aborts the
  request — the router instead returns its best-guess heuristic
  classification.
* **No content filters.**  The router never refuses any prompt and
  emits no legal disclaimers.  Infrastructure security is handled
  by ``code_intelligence/adversarial_reviewer.py``, not here.
"""

from __future__ import annotations

import logging
import re
from typing import Any, Awaitable, Callable

from .contracts import TaskComplexity, TaskIR

logger = logging.getLogger(__name__)


LLMCall = Callable[[str, str | None, int], Awaitable[str]]
EventCallback = Callable[[str, dict[str, Any]], Awaitable[None]] | None


# ─────────────────────────────────────────────────────────────────────
# Heuristic keyword sets.  Tuned to match the prompts that
# QuickCodeEngine sees in production.  Comments document why each
# group exists; if a keyword no longer fires for the right reason,
# delete it rather than rebalance.
# ─────────────────────────────────────────────────────────────────────


_TRIVIAL_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"\b(reverse|capitalize|upper.?case|lower.?case)\s+(?:a|the)\s+string\b", re.I),
    re.compile(r"\b(sum|add|count|max|min)\s+(?:a|the)\s+(list|array|sequence|numbers?)\b", re.I),
    re.compile(r"\bhello[,\s-]+world\b", re.I),
    re.compile(r"\bswap\s+(?:two|2)\s+(?:variables?|values?|numbers?)\b", re.I),
    re.compile(r"\bfactorial\b", re.I),
    re.compile(r"\bfizz.?buzz\b", re.I),
    re.compile(r"\bpalindrom(e|ic)\b", re.I),
)

_MATH_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"\b(integral|integrate|integrating)\b", re.I),
    re.compile(r"\b(derivative|differentiate|differentiation)\b", re.I),
    re.compile(r"\b(matrix|matrices|determinant|eigenvalue|eigenvector)\b", re.I),
    re.compile(r"\b(equation|inequalit(y|ies)|polynomial|quadratic|cubic|linear\s+system)\b", re.I),
    re.compile(r"\b(symbolic|sympy|sage|maxima)\b", re.I),
    re.compile(r"\b(proof|prove|theorem|lemma|corollary)\b", re.I),
    re.compile(r"\b(simplif(?:y|ication)|expand|factor(?:ize|ise)?)\s+(?:the\s+)?expression\b", re.I),
    re.compile(r"\b(numerical|stochastic)\s+(integration|differentiation|method)\b", re.I),
    re.compile(r"\bcalcul(?:us|ate)\s+(?:the\s+)?(integral|derivative|gradient)\b", re.I),
)

_COMPLEX_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"\bdistribut(ed|e|ing)\b", re.I),
    re.compile(r"\bmicroservices?\b", re.I),
    re.compile(r"\b(end[\s-]?to[\s-]?end|full[\s-]?stack)\b", re.I),
    re.compile(r"\b(production|enterprise|scal(?:e|able))[\s-]?(grade|ready)?\s+(system|service|platform|application)\b", re.I),
    re.compile(r"\boauth\s*2(?:\.0)?\b", re.I),
    re.compile(r"\bauthentication\s+(?:and|&|\+)\s+authorization\b", re.I),
    re.compile(r"\bbuild\s+(?:a|the)\s+(?:complete|whole|entire|full)\b", re.I),
    re.compile(r"\b(kafka|redis|kubernetes|k8s|terraform)\b", re.I),
    re.compile(r"\b(websocket|sse)\s+(?:streaming|server)\b", re.I),
    re.compile(r"\b(monorepo|cross[\s-]?service|cross[\s-]?repo)\b", re.I),
)


# Min length below which we always classify TRIVIAL.  Helps when the
# user types "fizz buzz" without context — the regex above catches it,
# but this is a backstop.
_TRIVIAL_MAX_CHARS = 80


# Max length above which we always classify COMPLEX.  A 4 000-char
# prompt is going to be a multi-component request even if no keyword
# fires.
_COMPLEX_MIN_CHARS = 1500


# ─────────────────────────────────────────────────────────────────────
# Heuristic classification
# ─────────────────────────────────────────────────────────────────────


def _heuristic(prompt: str) -> tuple[TaskComplexity | None, str]:
    """Return a ``(complexity, reason)`` pair.  ``complexity`` is
    ``None`` when the prompt is ambiguous and the LLM should be
    consulted.  ``reason`` is a short string that goes into the
    SSE event payload for transparency."""
    text = (prompt or "").strip()
    if not text:
        return TaskComplexity.TRIVIAL, "empty-prompt"

    if len(text) <= _TRIVIAL_MAX_CHARS:
        for pat in _TRIVIAL_PATTERNS:
            if pat.search(text):
                return TaskComplexity.TRIVIAL, f"trivial-keyword:{pat.pattern[:40]}"

    for pat in _MATH_PATTERNS:
        if pat.search(text):
            return TaskComplexity.MATH, f"math-keyword:{pat.pattern[:40]}"

    if len(text) >= _COMPLEX_MIN_CHARS:
        return TaskComplexity.COMPLEX, "long-prompt"
    for pat in _COMPLEX_PATTERNS:
        if pat.search(text):
            return TaskComplexity.COMPLEX, f"complex-keyword:{pat.pattern[:40]}"

    # Short prompts with no keyword hit are typically simple.
    if len(text) <= 240:
        return TaskComplexity.SIMPLE, "short-no-keyword"

    return None, "ambiguous"


# ─────────────────────────────────────────────────────────────────────
# LLM disambiguation
# ─────────────────────────────────────────────────────────────────────


_LLM_SYSTEM_PROMPT = (
    "You classify programming tasks into exactly one bucket. "
    "Return a single lowercase word and nothing else.\n\n"
    "Buckets:\n"
    "  trivial  - one-liner, fits in <30 LOC, no helpers needed\n"
    "  simple   - small standalone solution, <=100 LOC, <=3 helpers\n"
    "  complex  - multi-file/component, distributed system, full-stack\n"
    "  math     - symbolic, numerical, proof-heavy, algebra/calculus\n"
)


_LLM_OUTPUT_RE = re.compile(r"\b(trivial|simple|complex|math)\b", re.I)


def _parse_llm_response(text: str) -> TaskComplexity | None:
    if not text:
        return None
    m = _LLM_OUTPUT_RE.search(text)
    if not m:
        return None
    return TaskComplexity.coerce(m.group(1).lower())


# ─────────────────────────────────────────────────────────────────────
# Public class
# ─────────────────────────────────────────────────────────────────────


class TaskClassifier:
    """Hot-path task router.

    Args:
        llm_call:    Optional LLM bridge.  Falls back to the local
                     Ollama bridge in ``code_intelligence_routes``.
        model:       Ollama model tag.  Default ``qwen2.5:1.5b``.
        on_event:    Optional async callback so the engine can emit
                     SSE updates ``router_classified`` /
                     ``router_redirect_pro``.
        max_tokens:  LLM token cap.  We only need one word.
        redirect_to_pro:
                     When True (default) the classifier flags
                     COMPLEX tasks for redirect to the Pro engine.
    """

    REDIRECT_SENTINEL = "__QUICK_V2_REDIRECT_PRO__"

    def __init__(
        self,
        *,
        llm_call: LLMCall | None = None,
        model: str | None = None,
        on_event: EventCallback = None,
        max_tokens: int = 8,
        redirect_to_pro: bool = True,
    ) -> None:
        self._llm_call = llm_call
        self._model = model or "qwen2.5:1.5b"
        self._on_event = on_event
        self._max_tokens = max(1, int(max_tokens))
        self._redirect_to_pro = bool(redirect_to_pro)

    # ─── core entrypoints ────────────────────────────────────────────

    async def classify(
        self,
        prompt: str,
        language: str | None = None,
    ) -> TaskComplexity:
        """Return a ``TaskComplexity`` for ``prompt``.  Never raises."""
        del language  # reserved for future heuristics
        verdict, reason = _heuristic(prompt)
        if verdict is not None:
            await self._emit("router_classified", {
                "complexity": verdict.value,
                "source": "heuristic",
                "reason": reason,
            })
            return verdict

        # Heuristic was ambiguous — consult the small LLM.
        try:
            llm = await self._ensure_llm()
            raw = await llm(prompt, _LLM_SYSTEM_PROMPT, self._max_tokens)
        except Exception as exc:  # pragma: no cover - infra surface
            logger.warning("router LLM failed: %s — falling back to SIMPLE", exc)
            await self._emit("router_classified", {
                "complexity": TaskComplexity.SIMPLE.value,
                "source": "fallback",
                "reason": f"llm-error:{type(exc).__name__}",
            })
            return TaskComplexity.SIMPLE

        parsed = _parse_llm_response(raw)
        if parsed is None:
            await self._emit("router_classified", {
                "complexity": TaskComplexity.SIMPLE.value,
                "source": "fallback",
                "reason": "llm-unparseable",
            })
            return TaskComplexity.SIMPLE

        await self._emit("router_classified", {
            "complexity": parsed.value,
            "source": "llm",
            "reason": "llm-vote",
        })
        return parsed

    async def classify_ir(self, ir: TaskIR) -> TaskComplexity:
        """Convenience wrapper that mutates ``ir.complexity`` in place."""
        verdict = await self.classify(ir.prompt, ir.language)
        ir.complexity = verdict
        return verdict

    async def should_redirect_to_pro(
        self,
        complexity: TaskComplexity,
        request_mode: str,
    ) -> bool:
        """Returns True when the engine should short-circuit ``run()``
        with the redirect-to-pro sentinel."""
        if not self._redirect_to_pro:
            return False
        if complexity is not TaskComplexity.COMPLEX:
            return False
        if (request_mode or "").lower() == "pro":
            # Already in Pro mode — nothing to redirect.
            return False
        await self._emit("router_redirect_pro", {
            "from_mode": request_mode,
            "to": "/api/code/start",
        })
        return True

    # ─── internals ──────────────────────────────────────────────────

    async def _ensure_llm(self) -> LLMCall:
        if self._llm_call is None:
            # Lazy import — keeps the module load light for tests
            # that never instantiate a TaskClassifier.
            from ..api.code_intelligence_routes import _llm_call_local  # noqa: PLC0415

            self._llm_call = _llm_call_local
        return self._llm_call

    async def _emit(self, event: str, payload: dict[str, Any]) -> None:
        cb = self._on_event
        if cb is None:
            return
        try:
            await cb(event, payload)
        except Exception as exc:  # pragma: no cover - cosmetic
            logger.debug("router on_event(%s) failed: %s", event, exc)


__all__ = ["TaskClassifier", "LLMCall", "EventCallback"]
