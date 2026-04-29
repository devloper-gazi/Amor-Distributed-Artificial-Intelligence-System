"""
Code auditors — second-pass mesh that scrutinises the GENERATED code.

The reasoning mesh runs *before* code is written. Once the coder
agent produces actual code + tests, the auditors come in with the
same role-specific lens (math / performance / edge-case) and grade
the artifact. Their structured verdicts feed the meta-arbiter.

Each auditor returns a different JSON shape (because what's worth
flagging differs by lens), but they share an envelope so the engine
can treat them uniformly:

    {
      verdict: "approve" | "approve_with_changes" | "reject",
      confidence: 0..1,
      summary: str,
      <role-specific fields>
    }

Auditors run in parallel via ``asyncio.gather`` like the reasoning
specialists. A failed auditor is treated as a no-op (no penalty,
findings note the absence) — the meta-arbiter just sees fewer inputs.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Literal

from ..agents import _extract_json
from . import prompts as P
from .specialists import LLMCall, RoleSetter, SpecialistRoleId

logger = logging.getLogger(__name__)


_AUDITOR_PROMPTS: dict[SpecialistRoleId, str] = {
    "math":        P.MATH_CODE_AUDITOR_SYSTEM_PROMPT,
    "performance": P.PERFORMANCE_CODE_AUDITOR_SYSTEM_PROMPT,
    "edge_case":   P.EDGE_CASE_CODE_AUDITOR_SYSTEM_PROMPT,
}


_AUDITOR_ROUTING_KEYS: dict[SpecialistRoleId, str] = {
    "math":        "math_auditor",
    "performance": "performance_auditor",
    "edge_case":   "edge_case_auditor",
}


@dataclass
class AuditorOutput:
    """Single auditor's structured verdict."""

    role: SpecialistRoleId
    role_label: str
    verdict: Literal["approve", "approve_with_changes", "reject", "unknown"] = "unknown"
    confidence: float = 0.0
    summary: str = ""
    raw: str = ""
    payload: dict[str, Any] = field(default_factory=dict)
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "role": self.role,
            "role_label": self.role_label,
            "verdict": self.verdict,
            "confidence": round(float(self.confidence or 0.0), 4),
            "summary": self.summary,
            "payload": dict(self.payload),
            "error": self.error,
        }


@dataclass
class MeshCodeAudit:
    """Aggregate of all auditor outputs.

    Carries enough structure that:
      * the meta-arbiter prompt can include it verbatim,
      * the QuickCode bundle can persist it as JSON,
      * the engine can compute a quick "any reject?" signal without
        needing to re-parse anything.
    """

    auditors: list[AuditorOutput] = field(default_factory=list)
    findings: list[str] = field(default_factory=list)

    @property
    def any_rejected(self) -> bool:
        return any(a.verdict == "reject" for a in self.auditors)

    @property
    def any_changes_requested(self) -> bool:
        return any(
            a.verdict in {"approve_with_changes", "reject"}
            for a in self.auditors
        )

    @property
    def average_confidence(self) -> float:
        usable = [a.confidence for a in self.auditors
                  if a.error is None and a.verdict != "unknown"]
        return round(sum(usable) / len(usable), 4) if usable else 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "auditors": [a.to_dict() for a in self.auditors],
            "any_rejected": self.any_rejected,
            "any_changes_requested": self.any_changes_requested,
            "average_confidence": self.average_confidence,
            "findings": list(self.findings),
        }

    def by_role(self) -> dict[str, dict[str, Any]]:
        """Convenience for the meta-arbiter prompt."""
        return {a.role: a.to_dict() for a in self.auditors}


# ─── Base + role implementations ─────────────────────────────────────


class _BaseAuditor:
    role: SpecialistRoleId = "math"
    role_label: str = "Code Auditor"

    def __init__(
        self,
        llm_call: LLMCall,
        *,
        role_setter: RoleSetter | None = None,
        max_tokens: int = 1200,
    ) -> None:
        self._llm = llm_call
        self._role_setter = role_setter
        self._max_tokens = max_tokens

    @property
    def system_prompt(self) -> str:
        return _AUDITOR_PROMPTS[self.role]

    @property
    def routing_key(self) -> str:
        return _AUDITOR_ROUTING_KEYS[self.role]

    async def audit(
        self,
        *,
        user_prompt: str,
        code: str,
        tests: str | None = None,
        language: str = "python",
    ) -> AuditorOutput:
        if not (code or "").strip():
            return AuditorOutput(
                role=self.role, role_label=self.role_label,
                error="no code to audit",
            )
        prompt = P.code_auditor_user_prompt(user_prompt, code, tests, language)
        if self._role_setter is not None:
            try:
                self._role_setter(self.routing_key)
            except Exception:  # pragma: no cover
                logger.exception("auditor_role_setter_raised: %s", self.role)
        try:
            raw = await self._llm(prompt, self.system_prompt, self._max_tokens)
        except Exception as exc:
            logger.warning("auditor_%s_call_failed: %s", self.role, exc)
            return AuditorOutput(
                role=self.role, role_label=self.role_label,
                error=str(exc)[:300],
            )
        try:
            payload = _extract_json(raw or "")
        except ValueError as exc:
            logger.warning("auditor_%s_json_parse_failed: %s", self.role, exc)
            return AuditorOutput(
                role=self.role, role_label=self.role_label,
                raw=raw[:4000], error=f"JSON parse failed: {exc}"[:300],
            )

        verdict_raw = str(payload.get("verdict") or "").strip().lower()
        verdict: Literal["approve", "approve_with_changes", "reject", "unknown"] = (
            "approve"             if verdict_raw == "approve"
            else "approve_with_changes" if verdict_raw == "approve_with_changes"
            else "reject"         if verdict_raw == "reject"
            else "unknown"
        )
        confidence = self._clamp01(payload.get("confidence"))
        return AuditorOutput(
            role=self.role,
            role_label=self.role_label,
            verdict=verdict,
            confidence=confidence,
            summary=str(payload.get("summary") or "")[:600],
            raw=raw[:4000],
            payload=payload,
        )

    @staticmethod
    def _clamp01(v: Any) -> float:
        try:
            f = float(v or 0.0)
        except (TypeError, ValueError):
            return 0.0
        return max(0.0, min(1.0, f))


class MathCodeAuditor(_BaseAuditor):
    role: SpecialistRoleId = "math"
    role_label: str = "Mathematics Auditor"


class PerformanceCodeAuditor(_BaseAuditor):
    role: SpecialistRoleId = "performance"
    role_label: str = "Performance Auditor"


class EdgeCaseCodeAuditor(_BaseAuditor):
    role: SpecialistRoleId = "edge_case"
    role_label: str = "Edge-Case Auditor"


# ─── Parallel runner ────────────────────────────────────────────────


async def run_auditors_parallel(
    auditors: list[_BaseAuditor],
    *,
    user_prompt: str,
    code: str,
    tests: str | None = None,
    language: str = "python",
    timeout_s: float = 240.0,
) -> MeshCodeAudit:
    """Run every auditor concurrently. Bounded by ``timeout_s`` so a
    single hang doesn't block the whole pipeline."""
    if not auditors:
        return MeshCodeAudit(
            findings=["no auditors registered for the mesh"],
        )
    tasks = [
        asyncio.create_task(a.audit(
            user_prompt=user_prompt, code=code, tests=tests, language=language,
        ))
        for a in auditors
    ]
    try:
        results = await asyncio.wait_for(
            asyncio.gather(*tasks, return_exceptions=True),
            timeout=timeout_s,
        )
    except asyncio.TimeoutError:
        for t in tasks:
            if not t.done():
                t.cancel()
        return MeshCodeAudit(
            auditors=[
                AuditorOutput(
                    role=a.role, role_label=a.role_label,
                    error="auditor mesh timed out",
                )
                for a in auditors
            ],
            findings=["auditor mesh timed out"],
        )

    outputs: list[AuditorOutput] = []
    findings: list[str] = []
    for auditor, res in zip(auditors, results):
        if isinstance(res, BaseException):
            outputs.append(AuditorOutput(
                role=auditor.role, role_label=auditor.role_label,
                error=str(res)[:300],
            ))
            findings.append(f"{auditor.role_label} crashed: {res}")
        else:
            outputs.append(res)
            if res.error:
                findings.append(f"{auditor.role_label}: {res.error[:200]}")
            elif res.verdict in {"reject", "approve_with_changes"}:
                findings.append(
                    f"{auditor.role_label} ({res.verdict}, "
                    f"conf {res.confidence:.2f}): {res.summary[:160]}"
                )
    return MeshCodeAudit(auditors=outputs, findings=findings)
