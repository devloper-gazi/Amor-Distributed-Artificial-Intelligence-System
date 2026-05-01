"""
Sentinel — critic / re-auditor loop.

The Patcher proposes a fix.  We don't blindly trust it.  Instead:

1. Ask the Patcher for a complete replacement of the affected
   function / module.
2. Hand the patched code back to the Auditor for a re-audit.
3. If the Auditor still reports the original CWE on the same
   ``(file, line)``, loop with the new attempt fed back into the
   Patcher's context.
4. Cap iterations at ``max_iters`` (3 by default).  Return the last
   attempt regardless — the engine surfaces the iteration count so
   the user knows the fix may need human review.

Mirrors Quick Code V2's SeekerDebugger pattern but specialised for
security fixes.

License: MIT.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Awaitable, Callable

from .agents import AuditorAgent, PatcherAgent
from .models import AgentVerdict, Finding, RAGContext

logger = logging.getLogger(__name__)


@dataclass
class CriticResult:
    final_finding: Finding
    final_patched_code: str
    iterations: int
    history: list[dict] = field(default_factory=list)
    converged: bool = False
    raw_patches: list[str] = field(default_factory=list)


class CriticLoop:
    """Patcher ⟷ Re-Auditor iteration capped at ``max_iters``."""

    def __init__(
        self,
        *,
        patcher: PatcherAgent,
        auditor: AuditorAgent,
        max_iters: int = 3,
    ) -> None:
        self._patcher = patcher
        self._auditor = auditor
        self._max_iters = max(0, min(5, int(max_iters)))

    @property
    def max_iters(self) -> int:
        return self._max_iters

    async def refine(
        self,
        *,
        finding: Finding,
        auditor_summary: str,
        redteam_summary: str,
        code_excerpt: str,
        context: RAGContext,
    ) -> CriticResult:
        """Run up to ``max_iters`` Patcher → Re-Auditor cycles."""
        history: list[dict] = []
        raw_patches: list[str] = []
        current_code = code_excerpt
        last_patched_code = ""
        converged = False

        if self._max_iters == 0:
            return CriticResult(
                final_finding=finding,
                final_patched_code="",
                iterations=0,
                history=[],
                converged=False,
                raw_patches=[],
            )

        for it in range(1, self._max_iters + 1):
            # 1. Patcher proposes fix.
            try:
                patch = await self._patcher.patch(
                    finding=finding,
                    auditor_verdict=auditor_summary,
                    redteam_summary=redteam_summary,
                    code_excerpt=current_code,
                )
            except Exception as exc:  # pragma: no cover - infra
                logger.debug("critic patch attempt %s failed: %s", it, exc)
                history.append({
                    "iteration": it, "stage": "patch", "ok": False,
                    "error": f"{type(exc).__name__}",
                })
                break

            patched_code = patch.fix_diff or ""
            history.append({
                "iteration": it,
                "stage": "patch",
                "ok": bool(patched_code),
                "rationale": patch.rationale[:240],
            })
            if not patched_code:
                # Patcher gave up — stop without counting this as a
                # productive iteration.
                break
            raw_patches.append(patched_code)
            last_patched_code = patched_code

            # 2. Re-audit on the patched code.
            try:
                verdicts = await self._auditor.audit(
                    finding=finding,
                    context=context,
                    code_excerpt=patched_code,
                )
            except Exception as exc:  # pragma: no cover
                logger.debug("critic re-audit %s failed: %s", it, exc)
                history.append({
                    "iteration": it, "stage": "reaudit", "ok": False,
                    "error": f"{type(exc).__name__}",
                })
                break

            majority = AuditorAgent.majority_verdict(verdicts)
            history.append({
                "iteration": it,
                "stage": "reaudit",
                "ok": True,
                "verdict": majority.verdict,
                "confidence": majority.confidence,
                "rationale": majority.rationale[:240],
            })

            if majority.verdict in ("false_positive", "needs_more_context"):
                # Auditor no longer flags the original CWE → converged.
                converged = True
                break

            # Otherwise, feed the patched code back into the next
            # Patcher iteration's context.
            current_code = patched_code
            auditor_summary = (
                f"After iteration {it}, Auditor still flags the issue: "
                f"{majority.rationale[:600]}"
            )

        return CriticResult(
            final_finding=finding,
            final_patched_code=last_patched_code,
            iterations=len(raw_patches),
            history=history,
            converged=converged,
            raw_patches=raw_patches,
        )


__all__ = ["CriticLoop", "CriticResult"]
