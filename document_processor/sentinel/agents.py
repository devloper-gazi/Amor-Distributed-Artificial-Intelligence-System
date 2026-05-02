"""
Sentinel — five specialist LLM agents (Auditor, Reasoner, RedTeam,
Patcher, Judge).

Each agent inherits from ``_BaseAgent`` (mirrors the
``code_intelligence/agents.py`` pattern) and configures:
  * a per-role system prompt (constants in ``prompts.py``);
  * a per-role Ollama model tag;
  * a per-role temperature + max_tokens;
  * the LLM call adapter (defaults to the local Ollama bridge).

All agents are fail-soft: a malformed JSON or transient LLM error
returns an ``AgentVerdict(verdict="needs_more_context",
confidence=0.0)`` rather than raising — the Judge / scoring layer
treats that as "no signal".

License: MIT.
"""

from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from .models import (
    AgentRole,
    AgentVerdict,
    Finding,
    RAGContext,
    SeverityLevel,
    coerce_severity,
)
from .prompts import (
    AUDITOR_SYSTEM_PROMPT,
    JUDGE_SYSTEM_PROMPT,
    PATCHER_SYSTEM_PROMPT,
    REASONER_SYSTEM_PROMPT,
    REDTEAM_SYSTEM_PROMPT,
    auditor_prompt,
    judge_prompt,
    patcher_prompt,
    reasoner_prompt,
    redteam_prompt,
)

logger = logging.getLogger(__name__)


LLMCall = Callable[[str, str | None, int], Awaitable[str]]
"""Same signature used everywhere else in the repo:
``(prompt, system, max_tokens) -> str``."""


# ─────────────────────────────────────────────────────────────────────
# JSON parsing helper
# ─────────────────────────────────────────────────────────────────────


_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)\s*```", re.S | re.I)


def _parse_agent_json(text: str) -> dict[str, Any] | None:
    """Strip markdown fences if any, return the first JSON object."""
    if not text:
        return None
    fence = _FENCE_RE.search(text)
    blob = (fence.group(1) if fence else text).strip()
    if not blob:
        return None
    # Try direct parse first; fall back to extracting the first object.
    try:
        data = json.loads(blob)
        return data if isinstance(data, dict) else None
    except json.JSONDecodeError:
        pass
    # Greedy: find the first balanced { ... } in the blob.
    depth = 0
    start = -1
    for i, ch in enumerate(blob):
        if ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0 and start >= 0:
                try:
                    data = json.loads(blob[start : i + 1])
                    return data if isinstance(data, dict) else None
                except json.JSONDecodeError:
                    return None
    return None


# ─────────────────────────────────────────────────────────────────────
# Base agent
# ─────────────────────────────────────────────────────────────────────


@dataclass
class _BaseAgent:
    role: AgentRole = "auditor"
    system_prompt: str = ""
    model: str = "qwen2.5:7b"
    max_tokens: int = 1500
    temperature: float = 0.2
    llm_call: LLMCall | None = None
    # Phase 16 Commit F — optional Letta-style 3-tier memory hierarchy.
    # When ``None``, agents run without persistent memory (backwards-
    # compat with Phase 15 / V1 behaviour).  Inject a ``MemoryStore``
    # to enable core / recall / archival reads + writes from the
    # agent's prompt scaffolding without changing every call site.
    memory: Any | None = None

    async def _call(self, prompt: str) -> str:
        llm = self.llm_call or await self._default_llm()
        try:
            return await llm(prompt, self.system_prompt, self.max_tokens)
        except Exception as exc:  # pragma: no cover - infra path
            logger.debug(
                "sentinel agent %s LLM error: %s", self.role, exc
            )
            return ""

    async def _default_llm(self) -> LLMCall:
        # Lazy import to avoid circulars at module load.
        from ..api.code_intelligence_routes import _llm_call_local  # noqa: PLC0415
        return _llm_call_local

    async def _default_memory(self):
        """Lazy memory fallback — only constructed when an agent
        method actually reads or writes memory.  Returns a
        process-local ``MemoryStore`` rooted in a temp directory so
        an out-of-the-box agent has somewhere to write without a
        configured root.  Cached on the instance after first use."""
        if self.memory is not None:
            return self.memory
        try:
            from local_ai.memory import make_no_op_store  # noqa: PLC0415
            self.memory = make_no_op_store()
        except Exception as exc:  # pragma: no cover
            logger.debug(
                "sentinel agent %s memory fallback failed: %s",
                self.role, exc,
            )
            self.memory = None
        return self.memory


# ─────────────────────────────────────────────────────────────────────
# AuditorAgent — 3× voting
# ─────────────────────────────────────────────────────────────────────


class AuditorAgent(_BaseAgent):
    SYSTEM_PROMPT = AUDITOR_SYSTEM_PROMPT

    def __init__(
        self,
        *,
        llm_call: LLMCall | None = None,
        model: str = "qwen2.5-coder:7b",
        temperature: float = 0.2,
        max_tokens: int = 800,
        voting_n: int = 3,
    ) -> None:
        super().__init__(
            role="auditor",
            system_prompt=AUDITOR_SYSTEM_PROMPT,
            model=model,
            max_tokens=max_tokens,
            temperature=temperature,
            llm_call=llm_call,
        )
        self.voting_n = max(1, min(7, int(voting_n)))

    async def audit(
        self,
        finding: Finding,
        context: RAGContext,
        code_excerpt: str = "",
    ) -> list[AgentVerdict]:
        """Returns ``voting_n`` independent verdicts."""
        prompt = auditor_prompt(finding, context, code_excerpt)
        verdicts: list[AgentVerdict] = []
        for _ in range(self.voting_n):
            start = time.monotonic()
            raw = await self._call(prompt)
            elapsed = (time.monotonic() - start) * 1000.0
            verdicts.append(
                self._parse(raw, elapsed_ms=elapsed)
            )
        return verdicts

    def _parse(self, raw: str, *, elapsed_ms: float) -> AgentVerdict:
        data = _parse_agent_json(raw or "")
        if not data:
            return AgentVerdict(
                role="auditor",
                verdict="needs_more_context",
                confidence=0.0,
                rationale="auditor returned unparseable JSON",
                raw_llm_output=str(raw)[:4000],
                elapsed_ms=elapsed_ms,
            )
        return AgentVerdict(
            role="auditor",
            verdict=str(data.get("verdict") or "needs_more_context"),  # type: ignore[arg-type]
            confidence=float(data.get("confidence") or 0.0),
            rationale=str(data.get("rationale") or "")[:1200],
            suggested_severity=coerce_severity(data.get("suggested_severity"), default="low"),
            cwe=str(data.get("cwe") or ""),
            raw_llm_output=str(raw)[:4000],
            elapsed_ms=elapsed_ms,
        )

    @staticmethod
    def majority_verdict(verdicts: list[AgentVerdict]) -> AgentVerdict:
        """Pick the modal verdict; tie-break by highest confidence."""
        if not verdicts:
            return AgentVerdict(role="auditor")
        from collections import Counter
        tally = Counter(v.verdict for v in verdicts)
        modal, _count = tally.most_common(1)[0]
        candidates = [v for v in verdicts if v.verdict == modal]
        candidates.sort(key=lambda v: v.confidence, reverse=True)
        winner = candidates[0]
        # The merged confidence is the mean of the agreeing voters.
        winner.confidence = round(
            sum(v.confidence for v in candidates) / max(1, len(candidates)),
            4,
        )
        return winner


# ─────────────────────────────────────────────────────────────────────
# ReasonerAgent — single CoT call
# ─────────────────────────────────────────────────────────────────────


class ReasonerAgent(_BaseAgent):
    SYSTEM_PROMPT = REASONER_SYSTEM_PROMPT

    def __init__(
        self,
        *,
        llm_call: LLMCall | None = None,
        model: str = "qwen2.5:7b",
        temperature: float = 0.5,
        max_tokens: int = 1200,
    ) -> None:
        super().__init__(
            role="reasoner",
            system_prompt=REASONER_SYSTEM_PROMPT,
            model=model,
            max_tokens=max_tokens,
            temperature=temperature,
            llm_call=llm_call,
        )

    async def analyse(
        self,
        finding: Finding,
        auditor_summary: str,
        context: RAGContext,
        code_excerpt: str = "",
    ) -> AgentVerdict:
        prompt = reasoner_prompt(finding, auditor_summary, context, code_excerpt)
        start = time.monotonic()
        raw = await self._call(prompt)
        elapsed = (time.monotonic() - start) * 1000.0
        data = _parse_agent_json(raw or "") or {}
        return AgentVerdict(
            role="reasoner",
            verdict=str(data.get("verdict") or "needs_more_context"),  # type: ignore[arg-type]
            confidence=float(data.get("confidence") or 0.0),
            rationale=str(data.get("rationale") or "")[:1500],
            suggested_severity=coerce_severity(
                data.get("suggested_severity"), default="low"
            ),
            raw_llm_output=str(raw)[:6000],
            elapsed_ms=elapsed,
        )


# ─────────────────────────────────────────────────────────────────────
# RedTeamAgent
# ─────────────────────────────────────────────────────────────────────


class RedTeamAgent(_BaseAgent):
    SYSTEM_PROMPT = REDTEAM_SYSTEM_PROMPT

    def __init__(
        self,
        *,
        llm_call: LLMCall | None = None,
        model: str = "qwen2.5-coder:7b",
        temperature: float = 0.7,
        max_tokens: int = 1500,
    ) -> None:
        super().__init__(
            role="redteam",
            system_prompt=REDTEAM_SYSTEM_PROMPT,
            model=model,
            max_tokens=max_tokens,
            temperature=temperature,
            llm_call=llm_call,
        )

    async def attack(
        self,
        finding: Finding,
        context: RAGContext,
        code_excerpt: str = "",
    ) -> AgentVerdict:
        prompt = redteam_prompt(finding, context, code_excerpt)
        start = time.monotonic()
        raw = await self._call(prompt)
        elapsed = (time.monotonic() - start) * 1000.0
        data = _parse_agent_json(raw or "") or {}
        return AgentVerdict(
            role="redteam",
            verdict=str(data.get("verdict") or "needs_more_context"),  # type: ignore[arg-type]
            confidence=float(data.get("confidence") or 0.0),
            exploit_scenario=str(data.get("exploit_scenario") or "")[:2400],
            rationale="; ".join(
                str(p) for p in (data.get("preconditions") or [])
            )[:600],
            suggested_severity=coerce_severity(
                data.get("suggested_severity"), default="medium"
            ),
            raw_llm_output=str(raw)[:6000],
            elapsed_ms=elapsed,
        )


# ─────────────────────────────────────────────────────────────────────
# PatcherAgent
# ─────────────────────────────────────────────────────────────────────


class PatcherAgent(_BaseAgent):
    SYSTEM_PROMPT = PATCHER_SYSTEM_PROMPT

    def __init__(
        self,
        *,
        llm_call: LLMCall | None = None,
        model: str = "qwen2.5-coder:7b",
        temperature: float = 0.2,
        max_tokens: int = 2000,
    ) -> None:
        super().__init__(
            role="patcher",
            system_prompt=PATCHER_SYSTEM_PROMPT,
            model=model,
            max_tokens=max_tokens,
            temperature=temperature,
            llm_call=llm_call,
        )

    async def patch(
        self,
        finding: Finding,
        auditor_verdict: str,
        redteam_summary: str,
        code_excerpt: str,
    ) -> AgentVerdict:
        prompt = patcher_prompt(
            finding, auditor_verdict, redteam_summary, code_excerpt,
        )
        start = time.monotonic()
        raw = await self._call(prompt)
        elapsed = (time.monotonic() - start) * 1000.0
        data = _parse_agent_json(raw or "") or {}
        return AgentVerdict(
            role="patcher",
            verdict="approved" if data.get("patched_code") else "needs_more_context",
            confidence=float(0.6 if data.get("patched_code") else 0.0),
            rationale=str(data.get("rationale") or "")[:600],
            fix_diff=str(data.get("patched_code") or "")[:8000],
            raw_llm_output=str(raw)[:8000],
            elapsed_ms=elapsed,
        )


# ─────────────────────────────────────────────────────────────────────
# JudgeAgent
# ─────────────────────────────────────────────────────────────────────


class JudgeAgent(_BaseAgent):
    SYSTEM_PROMPT = JUDGE_SYSTEM_PROMPT

    def __init__(
        self,
        *,
        llm_call: LLMCall | None = None,
        model: str = "qwen2.5:7b",
        temperature: float = 0.0,
        max_tokens: int = 1500,
    ) -> None:
        super().__init__(
            role="judge",
            system_prompt=JUDGE_SYSTEM_PROMPT,
            model=model,
            max_tokens=max_tokens,
            temperature=temperature,
            llm_call=llm_call,
        )

    async def synthesize(
        self,
        finding: Finding,
        auditor_results: list[dict[str, Any]],
        reasoner_result: dict[str, Any] | None = None,
        redteam_result: dict[str, Any] | None = None,
    ) -> AgentVerdict:
        prompt = judge_prompt(
            finding, auditor_results, reasoner_result, redteam_result,
        )
        start = time.monotonic()
        raw = await self._call(prompt)
        elapsed = (time.monotonic() - start) * 1000.0
        data = _parse_agent_json(raw or "") or {}
        return AgentVerdict(
            role="judge",
            verdict=str(data.get("verdict") or "needs_more_context"),  # type: ignore[arg-type]
            confidence=float(data.get("confidence") or 0.0),
            rationale=str(data.get("rationale") or "")[:1200],
            suggested_severity=coerce_severity(
                data.get("final_severity"), default="low"
            ),
            raw_llm_output=str(raw)[:6000],
            elapsed_ms=elapsed,
        )


__all__ = [
    "AuditorAgent",
    "JudgeAgent",
    "LLMCall",
    "PatcherAgent",
    "ReasonerAgent",
    "RedTeamAgent",
]
