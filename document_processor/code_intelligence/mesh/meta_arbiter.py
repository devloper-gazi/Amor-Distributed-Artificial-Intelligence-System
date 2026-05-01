"""
MetaArbiterAgent — the final synthesiser of the Multi-ML Mesh.

After reasoning, implementation, verification, refinement, and the
code-auditor mesh have all run, the meta-arbiter sees everything at
once and produces the verdict the user actually cares about: is this
code production-ready, with what confidence, and what are the genuine
top risks and strengths.

The arbiter is calibrated — its `confidence` is meant to mean
"I would bet on this being correct". A `reject` verdict requires
concrete evidence (sandbox failure, audit reject, critical static
finding), not vibes.

Its output drives:
  * the QuickCode bundle's final verdict line
  * the production-readiness score (0..100) shown to the user
  * the meta-arbiter gate score in the QuickCode gates list
  * the self-evolution metrics (the arbiter's verdict is the ground-
    truth label we score the underlying specialists against)
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Literal

from ..agents import _extract_json
from . import prompts as P
from .code_auditors import MeshCodeAudit

logger = logging.getLogger(__name__)


LLMCall = Callable[[str, str | None, int], Awaitable[str]]
RoleSetter = Callable[[str | None], Any]


VerdictLiteral = Literal["approve", "approve_with_changes", "reject", "unknown"]


@dataclass
class MetaVerdict:
    """Structured verdict from the meta-arbiter."""

    verdict: VerdictLiteral = "unknown"
    confidence: float = 0.0
    production_readiness: float = 0.0
    top_risks: list[dict[str, str]] = field(default_factory=list)
    top_strengths: list[str] = field(default_factory=list)
    summary: str = ""
    raw: str = ""
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "verdict": self.verdict,
            "confidence": round(float(self.confidence or 0.0), 4),
            "production_readiness": round(float(self.production_readiness or 0.0), 1),
            "top_risks": list(self.top_risks),
            "top_strengths": list(self.top_strengths),
            "summary": self.summary,
            "error": self.error,
            # raw kept off the public dict — large debug payload.
        }


class MetaArbiterAgent:
    """Final synthesiser. One call per session."""

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

    async def arbitrate(
        self,
        *,
        user_prompt: str,
        chosen_summary: str,
        chosen_rationale: str,
        code: str,
        tests: str | None,
        execution_summary: str,
        static_summary: str,
        mesh_audit: MeshCodeAudit | None,
        refine_iterations: int,
    ) -> MetaVerdict:
        if self._role_setter is not None:
            try:
                self._role_setter("meta_arbiter")
            except Exception:  # pragma: no cover
                logger.exception("meta_arbiter_role_setter_raised")

        audit_reports = mesh_audit.by_role() if mesh_audit else {}
        prompt = P.meta_arbiter_user_prompt(
            user_prompt=user_prompt,
            chosen_summary=chosen_summary,
            chosen_rationale=chosen_rationale,
            code=code,
            tests=tests,
            execution_summary=execution_summary,
            static_summary=static_summary,
            audit_reports=audit_reports,
            refine_iterations=refine_iterations,
        )

        try:
            raw = await self._llm(prompt, P.META_ARBITER_SYSTEM_PROMPT, self._max_tokens)
        except Exception as exc:
            logger.warning("meta_arbiter_call_failed: %s", exc)
            return self._fallback_verdict(
                code=code, mesh_audit=mesh_audit,
                execution_summary=execution_summary,
                error=str(exc)[:300],
            )

        try:
            data = _extract_json(raw or "")
        except ValueError as exc:
            logger.warning("meta_arbiter_json_parse_failed: %s", exc)
            v = self._fallback_verdict(
                code=code, mesh_audit=mesh_audit,
                execution_summary=execution_summary,
                error=f"JSON parse failed: {exc}"[:300],
            )
            v.raw = (raw or "")[:4000]
            return v

        return self._parse(data, raw or "")

    def _parse(self, data: dict[str, Any], raw: str) -> MetaVerdict:
        verdict_raw = str(data.get("verdict") or "").strip().lower()
        verdict: VerdictLiteral = (
            "approve"             if verdict_raw == "approve"
            else "approve_with_changes" if verdict_raw == "approve_with_changes"
            else "reject"         if verdict_raw == "reject"
            else "unknown"
        )
        confidence = self._clamp01(data.get("confidence"))
        readiness = self._clamp_range(data.get("production_readiness"), 0.0, 100.0)

        risks_raw = data.get("top_risks") or []
        risks: list[dict[str, str]] = []
        for r in risks_raw[:5]:
            if isinstance(r, dict):
                sev = str(r.get("severity") or "medium").lower().strip()
                if sev not in {"high", "medium", "low"}:
                    sev = "medium"
                risks.append({
                    "severity": sev,
                    "description": str(r.get("description") or "")[:240],
                })
            elif isinstance(r, str):
                risks.append({"severity": "medium", "description": r[:240]})

        strengths_raw = data.get("top_strengths") or []
        strengths = [
            str(s)[:240] for s in strengths_raw[:5]
            if isinstance(s, (str, int, float)) and str(s).strip()
        ]

        return MetaVerdict(
            verdict=verdict,
            confidence=confidence,
            production_readiness=readiness,
            top_risks=risks,
            top_strengths=strengths,
            summary=str(data.get("summary") or "")[:400],
            raw=raw[:4000],
        )

    @staticmethod
    def _fallback_verdict(
        *,
        code: str,
        mesh_audit: MeshCodeAudit | None,
        execution_summary: str,
        error: str,
    ) -> MetaVerdict:
        """Deterministic fallback when the arbiter LLM is unavailable.

        Computes a verdict purely from the upstream signals so the user
        still gets a usable production-readiness score even when the
        meta-arbiter call failed. Conservative: anything below clean
        sandbox + no auditor reject becomes ``approve_with_changes``.
        """
        exec_ok = "ok" in (execution_summary or "").lower()
        any_reject = bool(mesh_audit and mesh_audit.any_rejected)
        any_changes = bool(mesh_audit and mesh_audit.any_changes_requested)

        if not (code or "").strip():
            verdict: VerdictLiteral = "reject"
            readiness = 30.0
            confidence = 0.5
        elif any_reject or not exec_ok:
            verdict = "reject" if any_reject else "approve_with_changes"
            readiness = 50.0
            confidence = 0.4
        elif any_changes:
            verdict = "approve_with_changes"
            readiness = 70.0
            confidence = 0.45
        else:
            verdict = "approve"
            readiness = 80.0
            confidence = 0.5  # capped — no LLM means we're not really sure

        risks: list[dict[str, str]] = []
        if any_reject:
            risks.append({
                "severity": "high",
                "description": "an auditor rejected the code; review their findings",
            })
        if not exec_ok:
            risks.append({
                "severity": "high",
                "description": "sandbox execution did not pass cleanly",
            })

        return MetaVerdict(
            verdict=verdict,
            confidence=confidence,
            production_readiness=readiness,
            top_risks=risks,
            top_strengths=[
                "deterministic fallback path used; LLM arbiter unavailable",
            ],
            summary=(
                "Deterministic fallback verdict — meta-arbiter LLM was "
                "not reachable. Verdict derived from sandbox + auditor "
                "signals."
            ),
            error=error,
        )

    @staticmethod
    def _clamp01(v: Any) -> float:
        try:
            f = float(v or 0.0)
        except (TypeError, ValueError):
            return 0.0
        return max(0.0, min(1.0, f))

    @staticmethod
    def _clamp_range(v: Any, lo: float, hi: float) -> float:
        try:
            f = float(v or 0.0)
        except (TypeError, ValueError):
            return lo
        return max(lo, min(hi, f))
