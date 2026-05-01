"""
Specialist reasoning agents for the Multi-ML Mesh.

Each specialist runs the SAME user prompt through a different system
prompt so the diversity comes from the lens, not the model weights.
When per-role model bindings are configured (e.g. coder → qwen2.5-
coder:7b) the specialist runs against that model; otherwise it falls
back to whatever the route resolved at session start.

Specialists are intentionally thin wrappers around the LLM call so
they're trivially mockable for tests. The actual scoring + merging
happens in the aggregator.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Literal

from ..agents import _extract_json
from . import prompts as P

logger = logging.getLogger(__name__)


LLMCall = Callable[[str, str | None, int], Awaitable[str]]
RoleSetter = Callable[[str | None], Any]


SpecialistRoleId = Literal["general", "math", "performance", "edge_case"]


# Mapping from specialist role → the system prompt it uses.
_REASONING_PROMPTS: dict[SpecialistRoleId, str] = {
    "general":     P.GENERAL_REASONER_SYSTEM_PROMPT,
    "math":        P.MATH_REASONER_SYSTEM_PROMPT,
    "performance": P.PERFORMANCE_REASONER_SYSTEM_PROMPT,
    "edge_case":   P.EDGE_CASE_REASONER_SYSTEM_PROMPT,
}


# Mapping from specialist role → the per-role identifier used by the
# routing layer (`_ACTIVE_ROLE` ContextVar). The router resolves these
# against the user's role-route config; unknown roles fall back to
# the active model. Keeps the mesh decoupled from any per-role model
# preferences the user configured globally.
_ROLE_ROUTING_KEYS: dict[SpecialistRoleId, str] = {
    "general":     "reasoner",          # same as QuickCode default
    "math":        "math_specialist",
    "performance": "performance_specialist",
    "edge_case":   "edge_case_specialist",
}


@dataclass
class SpecialistOutput:
    """One specialist's raw output before aggregation."""

    role: SpecialistRoleId
    role_label: str
    raw: str = ""
    parsed: dict[str, Any] = field(default_factory=dict)
    error: str | None = None
    model_tag: str | None = None  # filled by the role_setter when bound

    def alternatives(self) -> list[dict[str, Any]]:
        alts = self.parsed.get("alternatives") or []
        return [a for a in alts if isinstance(a, dict)]


# ─── Base + role implementations ─────────────────────────────────────


class _BaseSpecialist:
    """Common scaffolding — subclasses pick a role + system prompt."""

    role: SpecialistRoleId = "general"
    role_label: str = "General Reasoner"

    def __init__(
        self,
        llm_call: LLMCall,
        *,
        role_setter: RoleSetter | None = None,
        max_tokens: int = 1500,
    ) -> None:
        self._llm = llm_call
        self._role_setter = role_setter
        self._max_tokens = max_tokens

    @property
    def system_prompt(self) -> str:
        return _REASONING_PROMPTS[self.role]

    @property
    def routing_key(self) -> str:
        return _ROLE_ROUTING_KEYS[self.role]

    async def reason(
        self,
        *,
        user_prompt: str,
        code_context: str | None = None,
        triage: dict | None = None,
    ) -> SpecialistOutput:
        prompt = P.reasoning_user_prompt(user_prompt, code_context, triage)
        token = None
        if self._role_setter is not None:
            try:
                token = self._role_setter(self.routing_key)
            except Exception:  # pragma: no cover
                logger.exception("specialist_role_setter_raised: %s", self.role)

        try:
            raw = await self._llm(prompt, self.system_prompt, self._max_tokens)
        except Exception as exc:
            logger.warning("specialist_%s_call_failed: %s", self.role, exc)
            return SpecialistOutput(
                role=self.role, role_label=self.role_label,
                error=str(exc)[:300],
            )
        finally:
            # Best-effort role reset. If the setter is the real
            # ContextVar.set returning a token, the QuickCodeEngine's
            # _reset_role wraps that; here we just suppress.
            with contextlib.suppress(Exception):
                if token is not None and self._role_setter is not None:
                    # Some setters auto-reset; others need explicit
                    # token-based reset. We prefer not to clobber the
                    # active role globally — leave that to the caller.
                    pass

        try:
            parsed = _extract_json(raw or "")
        except ValueError as exc:
            logger.warning("specialist_%s_json_parse_failed: %s", self.role, exc)
            return SpecialistOutput(
                role=self.role, role_label=self.role_label,
                raw=raw[:4000], error=f"JSON parse failed: {exc}"[:300],
            )

        return SpecialistOutput(
            role=self.role, role_label=self.role_label,
            raw=raw[:4000], parsed=parsed,
        )


class GeneralReasonerAgent(_BaseSpecialist):
    role: SpecialistRoleId = "general"
    role_label: str = "General Reasoner"


class MathReasonerAgent(_BaseSpecialist):
    role: SpecialistRoleId = "math"
    role_label: str = "Mathematics Specialist"


class PerformanceReasonerAgent(_BaseSpecialist):
    role: SpecialistRoleId = "performance"
    role_label: str = "Performance Analyst"


class EdgeCaseReasonerAgent(_BaseSpecialist):
    role: SpecialistRoleId = "edge_case"
    role_label: str = "Edge-Case Hunter"


# ─── Parallel runner ────────────────────────────────────────────────


async def run_specialists_parallel(
    specialists: list[_BaseSpecialist],
    *,
    user_prompt: str,
    code_context: str | None = None,
    triage: dict | None = None,
    timeout_s: float = 240.0,
) -> list[SpecialistOutput]:
    """Run every specialist concurrently, return their outputs in
    the same order ``specialists`` was passed.

    Specialists that fail (LLM error, JSON parse error) return a
    ``SpecialistOutput`` with ``error`` populated and an empty
    ``parsed`` dict; the aggregator treats them as no-op contributors.
    A blanket timeout keeps a single hung specialist from blocking
    the whole mesh. The CPU-bound default model takes 60-90s per call
    so the default budget allows ~3 sequential rounds even on a slow
    host."""
    tasks = [
        asyncio.create_task(s.reason(
            user_prompt=user_prompt,
            code_context=code_context,
            triage=triage,
        ))
        for s in specialists
    ]
    try:
        results = await asyncio.wait_for(
            asyncio.gather(*tasks, return_exceptions=True),
            timeout=timeout_s,
        )
    except asyncio.TimeoutError:
        # Cancel anything still running and synthesise error outputs.
        for t in tasks:
            if not t.done():
                t.cancel()
        return [
            SpecialistOutput(
                role=s.role, role_label=s.role_label,
                error="specialist mesh timed out",
            )
            for s in specialists
        ]

    out: list[SpecialistOutput] = []
    for spec, res in zip(specialists, results):
        if isinstance(res, BaseException):
            logger.warning(
                "specialist_%s_unhandled: %s",
                spec.role, res,
            )
            out.append(SpecialistOutput(
                role=spec.role, role_label=spec.role_label,
                error=str(res)[:300],
            ))
        else:
            out.append(res)
    return out
