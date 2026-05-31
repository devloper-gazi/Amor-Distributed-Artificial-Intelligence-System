"""
Thinking Mode pipeline engine.

Orchestrates the six-phase reasoning pipeline (understand → decompose →
explore → evaluate → synthesize → critique) and emits structured SSE
events so the UI can show live progress.

Design notes
------------
* The engine is **LLM-agnostic**. The caller injects `llm_call`, so the
  same pipeline works against Ollama, Claude, or a mock.
* Phases are small and inspectable. Each produces a JSON artifact (except
  synthesize, which produces Markdown) that becomes part of the session
  snapshot.
* Robust JSON handling — local models routinely wrap their JSON in prose
  or Markdown fences. We strip both.
* Every phase is its own try/except so one bad phase can be marked failed
  without aborting the rest of the pipeline.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Dict, List, Literal, Optional

from . import prompts
from .models import Complexity, DeliverableKind

logger = logging.getLogger(__name__)


LLMCall = Callable[[str, Optional[str], int], Awaitable[str]]
EventCallback = Callable[[Dict[str, Any]], Awaitable[None]]

PhaseStatus = Literal["pending", "in_progress", "completed", "failed", "skipped"]


# ─────────────────────────────────────────────────────────────────────────────
# Phase token budgets per effort tier
# ─────────────────────────────────────────────────────────────────────────────

_EFFORT_BUDGETS: Dict[str, Dict[str, int]] = {
    "basic": {
        "analyze": 600,
        "understand": 600,
        "decompose": 500,
        "explore": 900,
        "evaluate": 500,
        "synthesize": 1400,
        "critique": 500,
    },
    "medium": {
        "analyze": 800,
        "understand": 900,
        "decompose": 700,
        "explore": 1400,
        "evaluate": 700,
        "synthesize": 2400,
        "critique": 700,
    },
    "deep": {
        "analyze": 1000,
        "understand": 1200,
        "decompose": 1000,
        "explore": 2000,
        "evaluate": 1000,
        "synthesize": 4000,
        "critique": 1000,
    },
    "expert": {
        "analyze": 1200,
        "understand": 1400,
        "decompose": 1200,
        "explore": 2400,
        "evaluate": 1200,
        "synthesize": 5000,
        "critique": 1200,
    },
    "ultra": {
        "analyze": 1500,
        "understand": 1800,
        "decompose": 1500,
        "explore": 3200,
        "evaluate": 1500,
        "synthesize": 6500,
        "critique": 1500,
    },
}

# Legacy → canonical tier names. Keeps old clients + persisted snapshots
# working after the rename.
_EFFORT_ALIAS: Dict[str, str] = {
    "quick": "basic",
    "fast": "basic",
    "standard": "medium",
    "balanced": "medium",
    "thorough": "deep",
    "comprehensive": "expert",
    "exhaustive": "ultra",
}


def _canonical_effort(effort: str) -> str:
    """Resolve alias → canonical tier; fall back to 'medium' on anything unknown."""
    if not effort:
        return "medium"
    key = str(effort).strip().lower()
    key = _EFFORT_ALIAS.get(key, key)
    return key if key in _EFFORT_BUDGETS else "medium"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class ThinkingPhase:
    name: str
    label: str
    status: PhaseStatus = "pending"
    detail: Dict[str, Any] = field(default_factory=dict)
    started_at: Optional[str] = None
    completed_at: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


# ─────────────────────────────────────────────────────────────────────────────
# JSON parsing helpers — local models love to wander
# ─────────────────────────────────────────────────────────────────────────────

_JSON_FENCE_RE = re.compile(r"```(?:json)?\s*(\{[\s\S]*?\})\s*```", re.MULTILINE)

# Reasoning models (qwen3:8b, deepseek-r1, …) emit a <think>…</think>
# chain-of-thought block before the answer.  That prose routinely
# contains stray { } braces, which wrecks the "widest balanced braces"
# fallback in _extract_json (it would span from a brace INSIDE the think
# block to the real closing brace and fail to parse) — the observed
# `thinking.phase_failed phase=decompose` JSON error.  Strip the block
# (closed, or truncated-open if generation was cut off) before parsing.
_THINK_BLOCK_RE = re.compile(r"<think\b[^>]*>[\s\S]*?(?:</think>|\Z)", re.IGNORECASE)


def _extract_json(raw: str) -> Dict[str, Any]:
    """
    Best-effort extraction of a JSON object from a model's text reply.

    Strategy: try a direct json.loads first; fall back to Markdown-fenced
    JSON; fall back to the widest balanced `{…}` span we can find. If all
    of those fail, raise ValueError so the caller can mark the phase failed.
    """
    if not raw:
        raise ValueError("empty model output")

    # Drop any reasoning-model <think>…</think> block first so its stray
    # braces can't corrupt the balanced-brace fallback below.
    raw = _THINK_BLOCK_RE.sub("", raw)

    # Try direct parse first
    stripped = raw.strip()
    try:
        return json.loads(stripped)
    except json.JSONDecodeError:
        pass

    # Try fenced ```json blocks
    fence_match = _JSON_FENCE_RE.search(stripped)
    if fence_match:
        try:
            return json.loads(fence_match.group(1))
        except json.JSONDecodeError:
            pass

    # Fall back to widest balanced braces
    first = stripped.find("{")
    last = stripped.rfind("}")
    if first != -1 and last != -1 and last > first:
        candidate = stripped[first : last + 1]
        try:
            return json.loads(candidate)
        except json.JSONDecodeError:
            # One more attempt: strip trailing commas which some local
            # models sprinkle in.
            cleaned = re.sub(r",(\s*[}\]])", r"\1", candidate)
            try:
                return json.loads(cleaned)
            except json.JSONDecodeError as exc:
                raise ValueError(f"could not parse model JSON: {exc}") from exc
    raise ValueError("no JSON object found in model output")


# ─────────────────────────────────────────────────────────────────────────────
# ThinkingEngine
# ─────────────────────────────────────────────────────────────────────────────


class ThinkingEngine:
    """
    Orchestrates a single thinking session end-to-end.

    One engine instance per session. Callers are expected to `await run()`
    exactly once; the engine persists progress via the injected
    `on_event` callback and returns the final session dict.
    """

    PHASE_PROGRESS = {
        "understand": 15,
        "decompose": 30,
        "explore": 55,
        "evaluate": 70,
        "synthesize": 90,
        "critique": 98,
    }

    def __init__(
        self,
        *,
        prompt: str,
        clarifications: Dict[str, str],
        deliverable: DeliverableKind,
        effort: Literal["basic", "medium", "deep", "expert", "ultra", "quick", "standard"],
        provider: Literal["local", "claude"],
        llm_call: LLMCall,
        on_event: Optional[EventCallback] = None,
        phase_timeout_s: Optional[float] = None,
        session_timeout_s: Optional[float] = None,
    ) -> None:
        self.prompt = prompt
        self.clarifications = clarifications or {}
        self.deliverable = deliverable
        # Normalize aliases (quick/standard/…) to the canonical 5-tier names.
        self.effort = _canonical_effort(effort)
        self.provider = provider
        self.llm_call = llm_call
        self._on_event = on_event or _noop_event
        self._budgets = _EFFORT_BUDGETS[self.effort]
        # v18.1.5 (Cycle H gate-gap fix) — per-phase + session wall-clock
        # caps to bound worst-case Sprint-0 latency.  Settings resolved
        # lazily so tests can construct an engine without booting the full
        # Pydantic settings tree; explicit kwargs override settings.
        self._phase_timeout_s = phase_timeout_s if phase_timeout_s is not None else _resolve_phase_timeout(self.effort)
        self._session_timeout_s = session_timeout_s if session_timeout_s is not None else _resolve_session_timeout(self.effort)
        self._session_started_monotonic: Optional[float] = None

        self.phases: List[ThinkingPhase] = [
            ThinkingPhase("understand", "Understanding"),
            ThinkingPhase("decompose", "Decomposing"),
            ThinkingPhase("explore", "Exploring alternatives"),
            ThinkingPhase("evaluate", "Evaluating & deciding"),
            ThinkingPhase("synthesize", "Synthesizing"),
            ThinkingPhase("critique", "Self-critique"),
        ]
        self._phase_index = {p.name: p for p in self.phases}

        # Results accumulated through the pipeline
        self.understanding: Dict[str, Any] = {}
        self.sub_questions: List[Dict[str, Any]] = []
        self.alternatives: List[Dict[str, Any]] = []
        self.decision: Dict[str, Any] = {}
        self.deliverable_markdown: str = ""
        self.critique: Dict[str, Any] = {}

    # ------------------------------------------------------------------ helpers

    async def _emit(self, event: Dict[str, Any]) -> None:
        try:
            await self._on_event(event)
        except Exception:
            # Never let a bad subscriber break the pipeline.
            logger.exception("thinking.on_event callback raised")

    async def _run_phase(
        self,
        name: str,
        runner: Callable[[], Awaitable[Dict[str, Any]]],
    ) -> Optional[Dict[str, Any]]:
        phase = self._phase_index[name]

        # v18.1.5 — global session budget short-circuit.  If we've already
        # spent the wall-clock budget on prior phases, skip this one with a
        # clear marker so the UI shows "skipped (session budget exhausted)"
        # rather than yet another timed-out phase.
        if self._session_timeout_s > 0 and self._session_started_monotonic is not None:
            elapsed = asyncio.get_event_loop().time() - self._session_started_monotonic
            if elapsed >= self._session_timeout_s:
                phase.status = "skipped"
                phase.completed_at = _now()
                phase.detail = {"reason": "session_budget_exhausted", "elapsed_s": round(elapsed, 1)}
                await self._emit(
                    {
                        "type": "phase_skipped",
                        "phase": name,
                        "label": phase.label,
                        "reason": "session_budget_exhausted",
                    }
                )
                return None

        phase.status = "in_progress"
        phase.started_at = _now()
        await self._emit({"type": "phase_start", "phase": name, "label": phase.label})

        # Clamp this phase's wall-clock cap to the REMAINING session budget
        # so the session can never overshoot `_session_timeout_s` by a full
        # phase.  Previously the session cap was only checked at phase
        # boundaries (above), so a phase that STARTED just under the cap
        # could still run a full `_phase_timeout_s` — pushing total
        # wall-clock to `session_timeout + phase_timeout` (e.g. 540 + 120 ≈
        # 660s).  In the Sprint-0 corpus this surfaced as Thinking prompts
        # hanging at the eval client's 600s cap (5 phases × 120s) and
        # registering as hard "no terminal event" timeouts instead of a
        # bounded, truncated result.  `min(phase, remaining)` makes
        # `_session_timeout_s` a hard ceiling.
        effective_timeout = self._phase_timeout_s
        if (
            self._session_timeout_s > 0
            and self._session_started_monotonic is not None
        ):
            remaining = self._session_timeout_s - (
                asyncio.get_event_loop().time() - self._session_started_monotonic
            )
            remaining = max(remaining, 0.0)
            effective_timeout = (
                min(self._phase_timeout_s, remaining)
                if self._phase_timeout_s > 0
                else remaining
            )

        try:
            # Per-phase wall-clock timeout, clamped to the remaining session
            # budget above.  0 → disabled (legacy; only when BOTH caps off).
            if effective_timeout > 0:
                result = await asyncio.wait_for(runner(), timeout=effective_timeout)
            else:
                result = await runner()
            phase.status = "completed"
            phase.completed_at = _now()
            phase.detail = result or {}
            await self._emit(
                {
                    "type": "phase_complete",
                    "phase": name,
                    "label": phase.label,
                    "detail": phase.detail,
                }
            )
            return result
        except asyncio.TimeoutError:
            phase.status = "failed"
            phase.completed_at = _now()
            phase.detail = {"error": f"timeout_after_{effective_timeout:.0f}s", "timed_out": True}
            logger.warning(
                "thinking.phase_timeout phase=%s budget=%.0fs (phase_cap=%.0fs session_cap=%.0fs)",
                name, effective_timeout, self._phase_timeout_s, self._session_timeout_s,
            )
            await self._emit(
                {
                    "type": "phase_failed",
                    "phase": name,
                    "label": phase.label,
                    "error": f"timeout_after_{effective_timeout:.0f}s",
                    "timed_out": True,
                }
            )
            return None
        except Exception as exc:
            phase.status = "failed"
            phase.completed_at = _now()
            phase.detail = {"error": str(exc)}
            logger.exception("thinking.phase_failed phase=%s", name)
            await self._emit(
                {
                    "type": "phase_failed",
                    "phase": name,
                    "label": phase.label,
                    "error": str(exc),
                }
            )
            return None

    async def _llm_json(self, phase: str, prompt: str) -> Dict[str, Any]:
        """Call the LLM and parse its reply as JSON, emitting a trace event."""
        max_tokens = self._budgets[phase]
        await self._emit({"type": "thinking", "phase": phase, "tokens_budget": max_tokens})
        raw = await self.llm_call(prompt, prompts.SYSTEM_PROMPT, max_tokens)
        return _extract_json(raw)

    # ------------------------------------------------------------------ phases

    async def _phase_understand(self) -> Dict[str, Any]:
        data = await self._llm_json(
            "understand",
            prompts.understand_prompt(self.prompt, self.clarifications),
        )
        # Clamp list sizes so one malformed phase can't balloon the UI.
        data = {
            "restatement": str(data.get("restatement", ""))[:2000],
            "constraints": list(map(str, data.get("constraints", [])))[:10],
            "preferences": list(map(str, data.get("preferences", [])))[:10],
            "assumptions": list(map(str, data.get("assumptions", [])))[:10],
            "unknowns": list(map(str, data.get("unknowns", [])))[:10],
        }
        self.understanding = data
        return data

    async def _phase_decompose(self) -> Dict[str, Any]:
        data = await self._llm_json(
            "decompose",
            prompts.decompose_prompt(self.prompt, self.clarifications, self.understanding),
        )
        raw_questions = data.get("sub_questions", [])
        questions: List[Dict[str, Any]] = []
        for i, q in enumerate(raw_questions[:7], start=1):
            if isinstance(q, dict) and q.get("question"):
                questions.append(
                    {
                        "index": int(q.get("index", i)),
                        "question": str(q["question"])[:500],
                        "why": str(q.get("why", ""))[:300],
                    }
                )
        self.sub_questions = questions
        return {"sub_questions": questions}

    async def _phase_explore(self) -> Dict[str, Any]:
        data = await self._llm_json(
            "explore",
            prompts.explore_prompt(self.prompt, self.understanding, self.sub_questions),
        )
        raw_alts = data.get("alternatives", [])
        alternatives: List[Dict[str, Any]] = []
        for alt in raw_alts[:4]:
            if not isinstance(alt, dict) or not alt.get("name"):
                continue
            alternatives.append(
                {
                    "id": str(alt.get("id") or _slug(alt["name"])),
                    "name": str(alt["name"])[:120],
                    "summary": str(alt.get("summary", ""))[:600],
                    "pros": list(map(str, alt.get("pros", [])))[:8],
                    "cons": list(map(str, alt.get("cons", [])))[:8],
                    "best_when": str(alt.get("best_when", ""))[:300],
                    "risk": _enum(alt.get("risk"), {"low", "medium", "high"}, "medium"),
                    "effort": _enum(alt.get("effort"), {"low", "medium", "high"}, "medium"),
                }
            )
        self.alternatives = alternatives
        return {"alternatives": alternatives}

    async def _phase_evaluate(self) -> Dict[str, Any]:
        if not self.alternatives:
            # Nothing to decide between — skip cleanly.
            self.decision = {"chosen_id": None, "justification": "no alternatives"}
            return self.decision
        data = await self._llm_json(
            "evaluate",
            prompts.evaluate_prompt(self.prompt, self.understanding, self.alternatives),
        )
        chosen_id = data.get("chosen_id")
        # Validate the chosen id actually exists; otherwise fall back to first.
        if not any(a["id"] == chosen_id for a in self.alternatives):
            chosen_id = self.alternatives[0]["id"]
        decision = {
            "chosen_id": chosen_id,
            "justification": str(data.get("justification", ""))[:1500],
            "key_trade_offs": list(map(str, data.get("key_trade_offs", [])))[:6],
            "confidence": _clamp_int(data.get("confidence"), 0, 100, 60),
            "would_reconsider_if": list(map(str, data.get("would_reconsider_if", [])))[:6],
        }
        self.decision = decision
        return decision

    async def _phase_synthesize(self) -> Dict[str, Any]:
        prompt = prompts.synthesize_prompt(
            self.prompt,
            self.understanding,
            self.alternatives,
            self.decision,
            self.deliverable,
        )
        max_tokens = self._budgets["synthesize"]
        await self._emit(
            {"type": "thinking", "phase": "synthesize", "tokens_budget": max_tokens}
        )
        markdown = await self.llm_call(prompt, prompts.SYSTEM_PROMPT, max_tokens)
        markdown = (markdown or "").strip()
        if not markdown:
            raise ValueError("synthesize returned empty output")
        self.deliverable_markdown = markdown
        # Hand the deliverable to subscribers early so the UI can start
        # rendering before the critique phase finishes.
        await self._emit(
            {
                "type": "deliverable_ready",
                "markdown": markdown,
                "deliverable": self.deliverable,
            }
        )
        return {"markdown": markdown, "deliverable": self.deliverable}

    async def _phase_critique(self) -> Dict[str, Any]:
        data = await self._llm_json(
            "critique",
            prompts.critique_prompt(
                self.prompt,
                self.understanding,
                self.decision,
                self.deliverable_markdown,
            ),
        )
        risks_raw = data.get("risks", [])
        risks: List[Dict[str, str]] = []
        for r in risks_raw[:6]:
            if not isinstance(r, dict):
                continue
            risks.append(
                {
                    "title": str(r.get("title", ""))[:120],
                    "detail": str(r.get("detail", ""))[:500],
                    "severity": _enum(r.get("severity"), {"low", "medium", "high"}, "medium"),
                }
            )
        critique = {
            "risks": risks,
            "open_questions": list(map(str, data.get("open_questions", [])))[:6],
            "next_steps": list(map(str, data.get("next_steps", [])))[:6],
            "confidence": _clamp_int(data.get("confidence"), 0, 100, 60),
        }
        self.critique = critique
        return critique

    # ------------------------------------------------------------------ run

    async def run(self) -> Dict[str, Any]:
        # v18.1.5 — anchor the session-wide wall-clock budget.  All
        # per-phase decisions check elapsed time against this anchor.
        self._session_started_monotonic = asyncio.get_event_loop().time()
        await self._run_phase("understand", self._phase_understand)
        if self.understanding:
            await self._run_phase("decompose", self._phase_decompose)
            await self._run_phase("explore", self._phase_explore)
            if self.alternatives:
                await self._run_phase("evaluate", self._phase_evaluate)
            else:
                self._phase_index["evaluate"].status = "skipped"
                self._phase_index["evaluate"].detail = {"reason": "no alternatives produced"}

            # Synthesis is the main deliverable — always attempt it, even if
            # some upstream phases produced nothing. The prompts are designed
            # to be tolerant of empty prior phases.
            await self._run_phase("synthesize", self._phase_synthesize)
            if self.deliverable_markdown:
                await self._run_phase("critique", self._phase_critique)
            else:
                self._phase_index["critique"].status = "skipped"
                self._phase_index["critique"].detail = {"reason": "no deliverable to critique"}
        else:
            # Understand failed outright — mark downstream phases skipped so
            # the UI doesn't show them stuck on "pending" forever.
            for name in ("decompose", "explore", "evaluate", "synthesize", "critique"):
                self._phase_index[name].status = "skipped"

        return self.snapshot()

    def snapshot(self) -> Dict[str, Any]:
        return {
            "phases": [p.to_dict() for p in self.phases],
            "understanding": self.understanding,
            "sub_questions": self.sub_questions,
            "alternatives": self.alternatives,
            "decision": self.decision,
            "deliverable_markdown": self.deliverable_markdown,
            "critique": self.critique,
        }


# ─────────────────────────────────────────────────────────────────────────────
# utilities
# ─────────────────────────────────────────────────────────────────────────────


async def _noop_event(_event: Dict[str, Any]) -> None:
    return None


# Effort-tier wall-clock scaling.  The flat settings
# (`code_thinking_{phase,session}_timeout_s`) define the CEILING — applied
# to the deepest tier — while lighter tiers get proportionally tighter caps
# so a "basic"/"medium" reasoning request returns fast and "deep"/"expert"/
# "ultra" keep room for genuinely long reasoning.  This aligns wall-clock
# latency with the user-selected reasoning DEPTH instead of one flat 540s
# budget for everyone (the Sprint-0 corpus runs "medium", which previously
# inherited the full 540s session budget and hit the runner's 600s cap).
# The setting always wins as an absolute ceiling, so operators can still
# cap globally or disable entirely (=0).
_SESSION_TIMEOUT_BY_TIER_S: Dict[str, float] = {
    "basic": 100.0, "medium": 180.0, "deep": 320.0, "expert": 450.0, "ultra": 540.0,
}
_PHASE_TIMEOUT_BY_TIER_S: Dict[str, float] = {
    "basic": 35.0, "medium": 55.0, "deep": 90.0, "expert": 110.0, "ultra": 120.0,
}


def _resolve_phase_timeout(effort: str = "ultra") -> float:
    """Resolve the per-phase wall-clock cap, scaled by effort tier and
    bounded by the ``code_thinking_phase_timeout_s`` setting (the ceiling).
    Falls back to the tier value if settings import fails (test envs);
    returns 0 to disable when the setting is non-positive."""
    tier_cap = _PHASE_TIMEOUT_BY_TIER_S.get(_canonical_effort(effort), 120.0)
    try:
        from ..config.settings import settings  # noqa: PLC0415
        ceiling = float(getattr(settings, "code_thinking_phase_timeout_s", 120.0))
    except Exception:
        return max(tier_cap, 0.0)
    if ceiling <= 0:
        return 0.0   # operator disabled the cap entirely
    return max(min(tier_cap, ceiling), 0.0)


def _resolve_session_timeout(effort: str = "ultra") -> float:
    """Resolve the per-session wall-clock cap, scaled by effort tier and
    bounded by the ``code_thinking_session_timeout_s`` setting (the ceiling,
    default 540s — 9 min, leaving headroom under the runner's 600s cap).
    Falls back to the tier value if settings import fails; 0 disables."""
    tier_cap = _SESSION_TIMEOUT_BY_TIER_S.get(_canonical_effort(effort), 540.0)
    try:
        from ..config.settings import settings  # noqa: PLC0415
        ceiling = float(getattr(settings, "code_thinking_session_timeout_s", 540.0))
    except Exception:
        return max(tier_cap, 0.0)
    if ceiling <= 0:
        return 0.0   # operator disabled the cap entirely
    return max(min(tier_cap, ceiling), 0.0)


def _slug(text: str) -> str:
    text = re.sub(r"[^a-zA-Z0-9]+", "-", text.strip().lower()).strip("-")
    return text[:40] or "option"


def _enum(value: Any, allowed: set, default: str) -> str:
    v = str(value or "").lower()
    return v if v in allowed else default


def _clamp_int(value: Any, lo: int, hi: int, default: int) -> int:
    try:
        n = int(value)
    except (TypeError, ValueError):
        return default
    return max(lo, min(hi, n))
