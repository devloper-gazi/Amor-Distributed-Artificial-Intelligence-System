"""
Sentinel — main orchestrator.

Walks the DAG (see docs/sentinel-architecture.md):

    normalize → static_swarm → ml_pipeline → aggregate
                 → rag_enrich → auditor (3x) → reasoner → redteam
                 → patcher → critic_loop → judge
                 → score → report

Every phase honours ``cancel_requested`` at its boundary.  Every
phase emits ``sentinel_phase_start`` / ``sentinel_phase_complete``
events through the injected ``on_event`` callback.  Failures inside
a phase are caught and logged as a ``sentinel_phase_failed`` event
plus a ``SentinelGate(status="failed")``; the pipeline keeps going
so the user gets a partial report rather than a silent abort.

License: MIT.
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Awaitable, Callable
from uuid import uuid4

from .agents import (
    AuditorAgent,
    JudgeAgent,
    PatcherAgent,
    ReasonerAgent,
    RedTeamAgent,
)
from .critic_loop import CriticLoop
from .ml_pipeline import MLPipeline
from .models import (
    AgentVerdict,
    Finding,
    SentinelBundle,
    SentinelGate,
    SentinelRequest,
    SeverityLevel,
    coerce_scan_profile,
    severity_rank,
)
from .rag import SentinelRAG
from .reporters import HTMLReporter, MarkdownReporter, SARIFReporter
from .score import (
    annotate_cvss,
    apply_merge,
    repo_risk_score,
    severity_histogram,
)
from .static_swarm import StaticSwarm

logger = logging.getLogger(__name__)


EventCallback = Callable[[dict[str, Any]], Awaitable[None]]
LLMCall = Callable[[str, str | None, int], Awaitable[str]]


async def _noop(_: dict[str, Any]) -> None:
    return None


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


# ─────────────────────────────────────────────────────────────────────
# Profile → which stages run
# ─────────────────────────────────────────────────────────────────────


PROFILE_STAGES: dict[str, dict[str, bool]] = {
    "quick": {
        "static_swarm": True,
        "ml_pipeline": True,
        "rag": False,
        "auditor": False,
        "reasoner": False,
        "redteam": False,
        "patcher": False,
        "critic_loop": False,
        "judge": False,
    },
    "standard": {
        "static_swarm": True,
        "ml_pipeline": True,
        "rag": True,
        "auditor": True,
        "reasoner": False,
        "redteam": False,
        "patcher": True,
        "critic_loop": True,
        "judge": True,
    },
    "deep": {
        "static_swarm": True,
        "ml_pipeline": True,
        "rag": True,
        "auditor": True,
        "reasoner": True,
        "redteam": True,
        "patcher": True,
        "critic_loop": True,
        "judge": True,
    },
    "paranoid": {
        "static_swarm": True,
        "ml_pipeline": True,
        "rag": True,
        "auditor": True,
        "reasoner": True,
        "redteam": True,
        "patcher": True,
        "critic_loop": True,
        "judge": True,
        # paranoid additionally enables self-play on top.
    },
}


# Cap how many findings get the full agent treatment; static_swarm
# can produce hundreds of low-severity hits and feeding them all to
# the LLM swarm would blow latency.
MAX_AGENT_REVIEWED_FINDINGS = 30


class SentinelEngine:
    """One instance per scan.  Inject everything for testability."""

    def __init__(
        self,
        *,
        request: SentinelRequest,
        session_id: str | None = None,
        on_event: EventCallback | None = None,
        llm_call: LLMCall | None = None,
        static_swarm: StaticSwarm | None = None,
        ml_pipeline: MLPipeline | None = None,
        rag: SentinelRAG | None = None,
        auditor: AuditorAgent | None = None,
        reasoner: ReasonerAgent | None = None,
        redteam: RedTeamAgent | None = None,
        patcher: PatcherAgent | None = None,
        judge: JudgeAgent | None = None,
        critic: CriticLoop | None = None,
    ) -> None:
        self.request = request.normalize()
        self.session_id = session_id or uuid4().hex
        self._on_event = on_event or _noop
        self._llm_call = llm_call

        self._stages = dict(PROFILE_STAGES.get(
            coerce_scan_profile(self.request.scan_profile),
            PROFILE_STAGES["standard"],
        ))
        self._apply_request_overrides()

        # Lazy slot for stages — constructed on first use unless the
        # caller injected a real instance (testing / dependency
        # injection).
        self._static = static_swarm
        self._ml = ml_pipeline
        self._rag = rag
        self._auditor = auditor
        self._reasoner = reasoner
        self._redteam = redteam
        self._patcher = patcher
        self._judge = judge
        self._critic = critic

        self.bundle = SentinelBundle(
            session_id=self.session_id,
            request=self.request,
            started_at=_now_iso(),
        )
        self._cancel_event = asyncio.Event()

    def _apply_request_overrides(self) -> None:
        """Per-request flags can disable individual stages."""
        if self.request.enable_static_swarm is not None:
            self._stages["static_swarm"] = bool(self.request.enable_static_swarm)
        if self.request.enable_ml_pipeline is not None:
            self._stages["ml_pipeline"] = bool(self.request.enable_ml_pipeline)
        if self.request.enable_rag is not None:
            self._stages["rag"] = bool(self.request.enable_rag)
        if self.request.enable_critic_loop is not None:
            self._stages["critic_loop"] = bool(self.request.enable_critic_loop)
        # self_play and judge are not exposed via request — settings only.

    # ─── Event helpers ───────────────────────────────────────────

    async def _emit(self, event: dict[str, Any]) -> None:
        try:
            stamped = {
                **event,
                "session_id": self.session_id,
                "ts": _now_iso(),
                "event_id": event.get("event_id") or uuid4().hex,
            }
            await self._on_event(stamped)
        except Exception:  # pragma: no cover
            logger.exception("sentinel.on_event raised")

    def _check_cancel(self) -> None:
        if self.request.cancel_requested or self._cancel_event.is_set():
            raise asyncio.CancelledError(
                f"Sentinel session {self.session_id} cancelled"
            )

    def cancel(self) -> None:
        self.request.cancel_requested = True
        self._cancel_event.set()

    # ─── run() ──────────────────────────────────────────────────

    async def run(self) -> SentinelBundle:
        start = time.monotonic()
        await self._emit({
            "type": "sentinel_started",
            "request": self.request.to_dict(),
        })
        try:
            self._check_cancel()
            await self._phase_normalize()
            self._check_cancel()
            await self._phase_static_swarm()
            self._check_cancel()
            await self._phase_ml_pipeline()
            self._check_cancel()
            await self._phase_aggregate()
            self._check_cancel()
            await self._phase_rag_enrich()
            self._check_cancel()
            await self._phase_agent_pipeline()
            self._check_cancel()
            await self._phase_critic_loop()
            self._check_cancel()
            await self._phase_judge()
            self._check_cancel()
            await self._phase_score()
            self._check_cancel()
            await self._phase_report()
        except asyncio.CancelledError:
            self.bundle.completed_at = _now_iso()
            self.bundle.elapsed_ms = (time.monotonic() - start) * 1000.0
            await self._emit({"type": "sentinel_cancelled"})
            return self.bundle
        except Exception as exc:
            logger.exception("sentinel_run_failed")
            self.bundle.completed_at = _now_iso()
            self.bundle.elapsed_ms = (time.monotonic() - start) * 1000.0
            await self._emit({
                "type": "sentinel_error",
                "error": f"{type(exc).__name__}: {exc}"[:400],
            })
            return self.bundle

        self.bundle.completed_at = _now_iso()
        self.bundle.elapsed_ms = (time.monotonic() - start) * 1000.0
        await self._emit({
            "type": "sentinel_completed",
            "findings_count": len(self.bundle.findings),
            "repo_risk_score": self.bundle.repo_risk_score,
            "elapsed_ms": self.bundle.elapsed_ms,
        })
        return self.bundle

    # ─── Phase: normalize ──────────────────────────────────────

    async def _phase_normalize(self) -> None:
        await self._emit({"type": "sentinel_phase_start", "phase": "normalize"})
        # Resolve absolute paths; collect a flat file list for ML.
        # Already de-duped by request.normalize().
        # Nothing further to do at V1; just record the gate.
        self.bundle.gates.append(SentinelGate(
            phase="normalize",
            status="passed",
            score=100.0,
            findings_count=0,
            summary=f"{len(self.request.paths)} paths normalised",
        ))
        await self._emit({
            "type": "sentinel_phase_complete", "phase": "normalize",
            "paths": len(self.request.paths),
        })

    # ─── Phase: static swarm ───────────────────────────────────

    async def _phase_static_swarm(self) -> None:
        if not self._stages.get("static_swarm", True):
            return
        await self._emit({"type": "sentinel_phase_start", "phase": "static_swarm"})
        sw = await self._get_static_swarm()
        try:
            res = await sw.scan(self.request.paths)
        except Exception as exc:
            logger.debug("sentinel static_swarm failed: %s", exc)
            self.bundle.gates.append(SentinelGate(
                phase="static_swarm", status="failed", score=0.0,
                summary=f"static_swarm error: {type(exc).__name__}",
            ))
            await self._emit({
                "type": "sentinel_phase_failed", "phase": "static_swarm",
                "error": f"{type(exc).__name__}",
            })
            return
        self.bundle.static_findings = res.findings
        self.bundle.tool_skipped = list(res.tools_skipped)
        self.bundle.gates.append(SentinelGate(
            phase="static_swarm",
            status="passed" if res.tools_run else "passed_warn",
            score=80.0 if res.tools_run else 40.0,
            findings_count=len(res.findings),
            summary=(f"{len(res.findings)} findings from "
                     f"{len(res.tools_run)} tool(s); "
                     f"{len(res.tools_skipped)} skipped"),
        ))
        await self._emit({
            "type": "sentinel_phase_complete", "phase": "static_swarm",
            "findings_count": len(res.findings),
            "tools_run": list(res.tools_run),
            "tools_skipped": list(res.tools_skipped),
        })

    # ─── Phase: ML pipeline ────────────────────────────────────

    async def _phase_ml_pipeline(self) -> None:
        if not self._stages.get("ml_pipeline", True):
            return
        await self._emit({"type": "sentinel_phase_start", "phase": "ml_pipeline"})
        ml = self._ml or MLPipeline()
        try:
            res = ml.scan_paths(self.request.paths)
        except Exception as exc:
            logger.debug("sentinel ml_pipeline failed: %s", exc)
            self.bundle.gates.append(SentinelGate(
                phase="ml_pipeline", status="failed", score=0.0,
                summary=f"ml_pipeline error: {type(exc).__name__}",
            ))
            return
        self.bundle.ml_findings = res.findings
        self.bundle.gates.append(SentinelGate(
            phase="ml_pipeline",
            status="passed",
            score=80.0,
            findings_count=len(res.findings),
            summary=(f"{len(res.findings)} ML findings "
                     f"(backend={res.backend_summary})"),
        ))
        await self._emit({
            "type": "sentinel_phase_complete", "phase": "ml_pipeline",
            "findings_count": len(res.findings),
            "backend": res.backend_summary,
        })

    # ─── Phase: aggregate ──────────────────────────────────────

    async def _phase_aggregate(self) -> None:
        await self._emit({"type": "sentinel_phase_start", "phase": "aggregate"})
        all_f = list(self.bundle.static_findings) + list(self.bundle.ml_findings)
        merged = apply_merge(all_f)
        annotated = annotate_cvss(merged)
        self.bundle.findings = annotated
        self.bundle.gates.append(SentinelGate(
            phase="aggregate", status="passed",
            score=90.0,
            findings_count=len(annotated),
            summary=f"{len(annotated)} findings after merge",
        ))
        await self._emit({
            "type": "sentinel_phase_complete", "phase": "aggregate",
            "findings_count": len(annotated),
        })

    # ─── Phase: RAG enrichment ─────────────────────────────────

    async def _phase_rag_enrich(self) -> None:
        if not self._stages.get("rag", True):
            return
        await self._emit({"type": "sentinel_phase_start", "phase": "rag_enrich"})
        rag = self._rag or SentinelRAG()
        try:
            await rag.ensure_loaded()
            for f in self._top_findings_for_agents():
                ctx = await rag.enrich(f)
                if ctx.cwe_entry and not f.cwe_name:
                    f.cwe_name = str(ctx.cwe_entry.get("name") or "")
                if ctx.owasp_entry and not f.owasp:
                    f.owasp = str(ctx.owasp_entry.get("id") or "")
            # Also seed the project context table with a few code chunks
            # so downstream `taint_trace`-style RAG queries have ground.
            for p in self.request.paths[:50]:
                try:
                    text = Path(p).read_text(encoding="utf-8", errors="replace")[:8000]
                    if text.strip():
                        await rag.index_project_chunk(
                            file=str(p), line_start=1, snippet=text[:1200],
                        )
                except Exception:
                    continue
        except Exception as exc:
            logger.debug("sentinel rag_enrich failed: %s", exc)
            self.bundle.gates.append(SentinelGate(
                phase="rag_enrich", status="passed_warn", score=50.0,
                summary=f"rag_enrich degraded: {type(exc).__name__}",
            ))
            return
        self.bundle.gates.append(SentinelGate(
            phase="rag_enrich", status="passed", score=85.0,
            summary="CWE/OWASP corpus + project context indexed",
        ))
        await self._emit({"type": "sentinel_phase_complete", "phase": "rag_enrich"})

    # ─── Phase: agent pipeline ─────────────────────────────────

    async def _phase_agent_pipeline(self) -> None:
        # Skip the entire phase when no agent stage is enabled in
        # the active profile (e.g. quick).  Avoids polluting the
        # gate timeline with a no-op agent_pipeline entry.
        if not any(
            self._stages.get(k, False)
            for k in ("auditor", "reasoner", "redteam")
        ):
            return
        targets = self._top_findings_for_agents()
        if not targets:
            return
        await self._emit({
            "type": "sentinel_phase_start", "phase": "agent_pipeline",
            "target_count": len(targets),
        })

        verdicts: dict[str, list[dict[str, Any]]] = {
            "auditor": [], "reasoner": [], "redteam": [],
            "patcher": [], "judge": [],
        }

        if self._stages.get("auditor", False):
            auditor = self._auditor or AuditorAgent(llm_call=self._llm_call)
            for f in targets:
                self._check_cancel()
                rag_ctx = await self._safe_rag_enrich(f)
                code_excerpt = await self._read_code_excerpt(f)
                try:
                    audit_results = await auditor.audit(
                        finding=f, context=rag_ctx, code_excerpt=code_excerpt,
                    )
                except Exception as exc:  # pragma: no cover
                    logger.debug("sentinel auditor failed: %s", exc)
                    continue
                majority = AuditorAgent.majority_verdict(audit_results)
                verdicts["auditor"].append(majority.to_dict())
                # Annotate the original finding with the auditor's view.
                f.confidence = max(f.confidence, majority.confidence)
                if severity_rank(majority.suggested_severity) > severity_rank(f.severity):
                    f.severity = majority.suggested_severity

                if self._stages.get("reasoner", False):
                    reasoner = self._reasoner or ReasonerAgent(llm_call=self._llm_call)
                    try:
                        rea = await reasoner.analyse(
                            finding=f, auditor_summary=majority.rationale,
                            context=rag_ctx, code_excerpt=code_excerpt,
                        )
                        verdicts["reasoner"].append(rea.to_dict())
                    except Exception as exc:  # pragma: no cover
                        logger.debug("sentinel reasoner failed: %s", exc)

                if self._stages.get("redteam", False):
                    redteam = self._redteam or RedTeamAgent(llm_call=self._llm_call)
                    try:
                        rt = await redteam.attack(
                            finding=f, context=rag_ctx, code_excerpt=code_excerpt,
                        )
                        verdicts["redteam"].append(rt.to_dict())
                        if rt.exploit_scenario:
                            f.extra = dict(f.extra or {})
                            f.extra["exploit_scenario"] = rt.exploit_scenario[:1200]
                    except Exception as exc:  # pragma: no cover
                        logger.debug("sentinel redteam failed: %s", exc)

        self.bundle.agent_verdicts = verdicts
        self.bundle.gates.append(SentinelGate(
            phase="agent_pipeline", status="passed", score=85.0,
            summary=(f"auditor={len(verdicts['auditor'])} "
                     f"reasoner={len(verdicts['reasoner'])} "
                     f"redteam={len(verdicts['redteam'])}"),
        ))
        await self._emit({
            "type": "sentinel_phase_complete", "phase": "agent_pipeline",
            "auditor_count": len(verdicts["auditor"]),
            "reasoner_count": len(verdicts["reasoner"]),
            "redteam_count": len(verdicts["redteam"]),
        })

    # ─── Phase: critic loop ────────────────────────────────────

    async def _phase_critic_loop(self) -> None:
        if not self._stages.get("critic_loop", False):
            return
        if not self._stages.get("patcher", False):
            return
        await self._emit({"type": "sentinel_phase_start", "phase": "critic_loop"})
        patcher = self._patcher or PatcherAgent(llm_call=self._llm_call)
        auditor = self._auditor or AuditorAgent(llm_call=self._llm_call)
        critic = self._critic or CriticLoop(
            patcher=patcher, auditor=auditor, max_iters=3,
        )
        targets = [
            f for f in self._top_findings_for_agents()
            if severity_rank(f.severity) >= severity_rank("medium")
        ][:5]   # cap critic loop to top-5 to keep latency bounded
        total_iters = 0
        patches: list[dict[str, Any]] = []
        for f in targets:
            self._check_cancel()
            rag_ctx = await self._safe_rag_enrich(f)
            code_excerpt = await self._read_code_excerpt(f)
            try:
                res = await critic.refine(
                    finding=f,
                    auditor_summary=(f.extra or {}).get("auditor_summary", ""),
                    redteam_summary=(f.extra or {}).get("exploit_scenario", ""),
                    code_excerpt=code_excerpt,
                    context=rag_ctx,
                )
            except Exception as exc:  # pragma: no cover
                logger.debug("sentinel critic_loop failed: %s", exc)
                continue
            total_iters += res.iterations
            if res.final_patched_code:
                f.extra = dict(f.extra or {})
                f.extra["fix_diff"] = res.final_patched_code[:4000]
                f.extra["fix_converged"] = res.converged
                patches.append({
                    "fingerprint": f.fingerprint,
                    "iterations": res.iterations,
                    "converged": res.converged,
                })
        self.bundle.critic_iterations = total_iters
        self.bundle.agent_verdicts.setdefault("patcher", []).extend(patches)
        self.bundle.gates.append(SentinelGate(
            phase="critic_loop", status="passed", score=80.0,
            summary=f"{len(patches)} patches across {total_iters} iterations",
        ))
        await self._emit({
            "type": "sentinel_phase_complete", "phase": "critic_loop",
            "patches": len(patches), "total_iters": total_iters,
        })

    # ─── Phase: judge ─────────────────────────────────────────

    async def _phase_judge(self) -> None:
        if not self._stages.get("judge", False):
            return
        if not self.bundle.findings:
            return
        await self._emit({"type": "sentinel_phase_start", "phase": "judge"})
        judge = self._judge or JudgeAgent(llm_call=self._llm_call)
        verdicts: list[dict[str, Any]] = []
        targets = self._top_findings_for_agents()[:10]   # cap
        for f in targets:
            self._check_cancel()
            try:
                verdict = await judge.synthesize(
                    finding=f,
                    auditor_results=self.bundle.agent_verdicts.get("auditor", []),
                    reasoner_result=(self.bundle.agent_verdicts.get("reasoner") or [None])[0],
                    redteam_result=(self.bundle.agent_verdicts.get("redteam") or [None])[0],
                )
                verdicts.append(verdict.to_dict())
                # Update final severity from judge if it disagrees upward.
                if severity_rank(verdict.suggested_severity) > severity_rank(f.severity):
                    f.severity = verdict.suggested_severity
            except Exception as exc:  # pragma: no cover
                logger.debug("sentinel judge failed: %s", exc)
                continue
        self.bundle.agent_verdicts["judge"] = verdicts
        self.bundle.gates.append(SentinelGate(
            phase="judge", status="passed", score=90.0,
            summary=f"{len(verdicts)} judge verdicts",
        ))
        await self._emit({"type": "sentinel_phase_complete", "phase": "judge"})

    # ─── Phase: score ─────────────────────────────────────────

    async def _phase_score(self) -> None:
        await self._emit({"type": "sentinel_phase_start", "phase": "score"})
        hist = severity_histogram(self.bundle.findings)
        risk = repo_risk_score(
            self.bundle.findings,
            file_count=max(1, len(self.request.paths) or 1),
        )
        self.bundle.severity_histogram = hist
        self.bundle.repo_risk_score = risk
        self.bundle.gates.append(SentinelGate(
            phase="score", status="passed",
            score=max(0.0, 100.0 - risk * 10.0),
            summary=f"repo_risk_score={risk:.2f}",
        ))
        await self._emit({
            "type": "sentinel_phase_complete", "phase": "score",
            "histogram": hist, "repo_risk_score": risk,
        })

    # ─── Phase: report ────────────────────────────────────────

    async def _phase_report(self) -> None:
        await self._emit({"type": "sentinel_phase_start", "phase": "report"})
        try:
            self.bundle.sarif_report = SARIFReporter().render(self.bundle)
            self.bundle.markdown_report = MarkdownReporter().render(self.bundle)
            self.bundle.html_report = HTMLReporter().render(self.bundle)
        except Exception as exc:
            logger.debug("sentinel report failed: %s", exc)
            self.bundle.gates.append(SentinelGate(
                phase="report", status="failed", score=0.0,
                summary=f"report error: {type(exc).__name__}",
            ))
            return
        self.bundle.gates.append(SentinelGate(
            phase="report", status="passed", score=100.0,
            summary="SARIF + MD + HTML rendered",
        ))
        await self._emit({"type": "sentinel_phase_complete", "phase": "report"})

    # ─── Lazy stage helpers ──────────────────────────────────

    async def _get_static_swarm(self) -> StaticSwarm:
        if self._static is not None:
            return self._static
        # Pick tools by profile — quick uses 2, standard 4, deep all.
        prof = self.request.scan_profile
        if prof == "quick":
            tools = StaticSwarm.DEFAULT_QUICK_TOOLS
        elif prof in ("standard",):
            tools = StaticSwarm.DEFAULT_STANDARD_TOOLS
        else:
            tools = StaticSwarm.DEFAULT_DEEP_TOOLS

        async def _bridge(event: str, payload: dict[str, Any]) -> None:
            await self._emit({"type": f"sentinel_{event}", **payload})

        self._static = StaticSwarm(tools=tools, on_event=_bridge)
        return self._static

    def _top_findings_for_agents(self) -> list[Finding]:
        """Top-N highest severity findings the LLM swarm reviews."""
        sorted_f = sorted(
            self.bundle.findings,
            key=lambda f: (severity_rank(f.severity), f.confidence),
            reverse=True,
        )
        return sorted_f[: MAX_AGENT_REVIEWED_FINDINGS]

    async def _safe_rag_enrich(self, f: Finding):
        if self._rag is None:
            from .models import RAGContext
            return RAGContext()
        try:
            return await self._rag.enrich(f)
        except Exception:
            from .models import RAGContext
            return RAGContext()

    async def _read_code_excerpt(self, f: Finding) -> str:
        if not f.file:
            return ""
        try:
            text = Path(f.file).read_text(encoding="utf-8", errors="replace")
        except Exception:
            return ""
        lines = text.splitlines()
        if not lines:
            return ""
        center = max(1, f.line_start)
        start = max(1, center - 6)
        end = min(len(lines), center + 12)
        return "\n".join(lines[start - 1: end])


__all__ = ["MAX_AGENT_REVIEWED_FINDINGS", "PROFILE_STAGES", "SentinelEngine"]
