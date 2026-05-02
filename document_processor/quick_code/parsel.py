"""
QuickCode V2 — ParselDecomposer (divide-and-conquer + DBC).

Inspired by `Parsel <https://arxiv.org/abs/2212.10561>`_, this module
asks a small LLM to break a high-level task into ≤ N sub-tasks, each
with a pre-condition and a post-condition.  The result populates
``TaskIR.subtasks`` so downstream phases (reasoner, coder, seeker)
can target one focused sub-task at a time.

Behaviour
---------

* Single-level decomposition by default (``max_depth=2`` keeps a
  small recursion budget without exploding the LLM call count).
* ``decompose_short_circuit_floor`` (default 200 chars).  Prompts
  shorter than the floor skip the LLM and return ``ir`` untouched —
  there's nothing to decompose.
* Fail-soft: when the LLM call raises or returns un-parseable JSON
  the decomposer leaves ``ir.subtasks`` empty rather than blocking
  the rest of the pipeline.
* No content filters / refusal language in the prompt template.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any, Awaitable, Callable

from .contracts import ContractCondition, SubTask, TaskIR

logger = logging.getLogger(__name__)


LLMCall = Callable[[str, str | None, int], Awaitable[str]]
EventCallback = Callable[[str, dict[str, Any]], Awaitable[None]] | None


# ─────────────────────────────────────────────────────────────────────
# Prompt
#
# Strict JSON output, no prose.  The expert-tone preamble matches
# the rest of the V2 stack (no refusals, no legal disclaimers).
# ─────────────────────────────────────────────────────────────────────


PARSEL_SYSTEM_PROMPT = """You are an expert software engineer.

You decompose a programming task into 2-5 atomic sub-tasks.
Each sub-task has:

  - id           : short snake_case identifier, unique within the response
  - title        : <= 80 chars, imperative tense
  - description  : <= 400 chars, what the sub-task should produce
  - contract_pre : list of pre-conditions that must hold on entry
  - contract_post: list of post-conditions that must hold on exit
  - dependencies : list of sub-task ids that must complete first

Each contract item is {"kind": "pre"|"post", "expression": "<short>",
"description": "<<=200 chars>"}. Use plain Python-like expressions
without I/O. Direct, technical phrasing. No legal disclaimers, no
refusal language.

Return STRICT JSON, no prose, no markdown fences, exactly:

{
  "subtasks": [
    {
      "id": "...",
      "title": "...",
      "description": "...",
      "contract_pre":  [{"kind":"pre",  "expression":"...", "description":"..."}],
      "contract_post": [{"kind":"post", "expression":"...", "description":"..."}],
      "dependencies": ["other_id"]
    }
  ]
}
"""


# ─────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────


_JSON_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)\s*```", re.S | re.I)


def _parse_json(text: str) -> dict[str, Any] | None:
    if not text:
        return None
    fenced = _JSON_FENCE_RE.search(text)
    blob = fenced.group(1) if fenced else text
    try:
        data = json.loads(blob)
    except json.JSONDecodeError:
        # Sometimes the model emits multiple JSON objects; take the first.
        first = _first_json_object(blob)
        if first is None:
            return None
        try:
            data = json.loads(first)
        except json.JSONDecodeError:
            return None
    return data if isinstance(data, dict) else None


def _first_json_object(text: str) -> str | None:
    depth = 0
    start = -1
    for i, ch in enumerate(text):
        if ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0 and start >= 0:
                return text[start : i + 1]
    return None


def _coerce_condition(raw: Any, *, kind: str) -> ContractCondition | None:
    if not isinstance(raw, dict):
        return None
    expr = str(raw.get("expression") or "").strip()
    if not expr:
        return None
    return ContractCondition(
        kind=raw.get("kind") or kind,  # type: ignore[arg-type]
        expression=expr[:2000],
        description=str(raw.get("description") or "")[:2000],
    )


def _coerce_subtask(raw: Any) -> SubTask | None:
    if not isinstance(raw, dict):
        return None
    sid = str(raw.get("id") or "").strip()
    title = str(raw.get("title") or "").strip()
    if not sid or not title:
        return None
    pre = [
        c
        for c in (
            _coerce_condition(x, kind="pre")
            for x in (raw.get("contract_pre") or [])
        )
        if c is not None
    ]
    post = [
        c
        for c in (
            _coerce_condition(x, kind="post")
            for x in (raw.get("contract_post") or [])
        )
        if c is not None
    ]
    deps_raw = raw.get("dependencies") or []
    deps: list[str] = []
    seen: set[str] = set()
    for d in deps_raw:
        s = str(d or "").strip()
        if s and s not in seen and s != sid:
            seen.add(s)
            deps.append(s)
    try:
        return SubTask(
            id=sid[:200],
            title=title[:400],
            description=str(raw.get("description") or "")[:4000],
            contract_pre=pre,
            contract_post=post,
            dependencies=deps,
        )
    except Exception as exc:  # pragma: no cover
        logger.debug("parsel subtask validation failed: %s", exc)
        return None


def _drop_invalid_dependencies(subtasks: list[SubTask]) -> list[SubTask]:
    """The TaskIR validator rejects any sub-task whose dependency
    points at an unknown id — strip those out so the LLM emitting a
    typo cannot poison the graph."""
    valid_ids = {st.id for st in subtasks}
    cleaned: list[SubTask] = []
    for st in subtasks:
        kept = [d for d in st.dependencies if d in valid_ids]
        if kept != st.dependencies:
            try:
                st = st.model_copy(update={"dependencies": kept})
            except Exception:  # pragma: no cover
                continue
        cleaned.append(st)
    return cleaned


# ─────────────────────────────────────────────────────────────────────
# Public class
# ─────────────────────────────────────────────────────────────────────


class ParselDecomposer:
    """LLM-driven divide-and-conquer + DBC decomposer."""

    SYSTEM_PROMPT = PARSEL_SYSTEM_PROMPT

    def __init__(
        self,
        *,
        llm_call: LLMCall | None = None,
        on_event: EventCallback = None,
        max_depth: int = 2,
        max_subtasks: int = 5,
        short_circuit_chars: int = 200,
        max_tokens: int = 1500,
    ) -> None:
        self._llm_call = llm_call
        self._on_event = on_event
        self._max_depth = max(1, min(3, int(max_depth)))
        self._max_subtasks = max(1, min(10, int(max_subtasks)))
        self._short_circuit_chars = max(0, int(short_circuit_chars))
        self._max_tokens = max(256, int(max_tokens))

    async def decompose(self, ir: TaskIR) -> TaskIR:
        """Mutate ``ir`` in place by populating ``ir.subtasks``.

        Returns the same ``ir`` for chaining."""
        if ir.subtasks:
            # Already decomposed by an upstream caller — leave it.
            return ir
        prompt = (ir.prompt or "").strip()
        if len(prompt) < self._short_circuit_chars:
            await self._emit("parsel_short_circuit", {
                "reason": "prompt-too-short",
                "len": len(prompt),
            })
            return ir
        try:
            llm = await self._ensure_llm()
            raw = await llm(prompt, self.SYSTEM_PROMPT, self._max_tokens)
        except Exception as exc:  # pragma: no cover - infra
            logger.debug("parsel LLM failed: %s", exc)
            await self._emit("parsel_failed", {"error": f"{type(exc).__name__}"})
            return ir

        parsed = _parse_json(raw or "")
        if not parsed:
            await self._emit("parsel_failed", {"error": "unparseable-json"})
            return ir

        subtasks_raw = parsed.get("subtasks") or []
        coerced = [_coerce_subtask(x) for x in subtasks_raw]
        subtasks = [s for s in coerced if s is not None][: self._max_subtasks]
        subtasks = _drop_invalid_dependencies(subtasks)

        if not subtasks:
            await self._emit("parsel_failed", {"error": "no-valid-subtasks"})
            return ir

        try:
            ir.subtasks = subtasks  # triggers TaskIR's validator
        except Exception as exc:
            logger.debug("parsel subtasks rejected by TaskIR validator: %s", exc)
            await self._emit("parsel_failed", {
                "error": "ir-validation-failed",
                "detail": str(exc)[:200],
            })
            return ir

        await self._emit("parsel_decomposed", {
            "count": len(subtasks),
            "ids": [s.id for s in subtasks],
        })
        return ir

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
        except Exception as exc:  # pragma: no cover - cosmetic
            logger.debug("parsel on_event(%s) failed: %s", event, exc)


__all__ = ["ParselDecomposer", "PARSEL_SYSTEM_PROMPT"]
