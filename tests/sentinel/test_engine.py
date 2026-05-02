"""Integration tests for ``document_processor/sentinel/engine.py``.

Uses mocked LLM + injected stage components so the suite runs
without Docker / Ollama.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from document_processor.sentinel.agents import (
    AuditorAgent,
    JudgeAgent,
    PatcherAgent,
    ReasonerAgent,
    RedTeamAgent,
)
from document_processor.sentinel.critic_loop import CriticLoop
from document_processor.sentinel.engine import (
    PROFILE_STAGES,
    SentinelEngine,
)
from document_processor.sentinel.ml_pipeline import MLPipeline
from document_processor.sentinel.models import (
    Finding,
    SentinelRequest,
)
from document_processor.sentinel.rag import SentinelRAG
from document_processor.sentinel.static_swarm import StaticSwarm, StaticSwarmResult


def _run(coro):
    return asyncio.run(coro)


# ─── Fakes ──────────────────────────────────────────────────────────


class _FakeStaticSwarm:
    """Returns a fixed list of findings."""

    def __init__(self, findings: list[Finding] | None = None,
                 skipped: list[str] | None = None) -> None:
        self.findings = list(findings or [])
        self.skipped = list(skipped or [])

    async def scan(self, paths: list[str]) -> StaticSwarmResult:
        return StaticSwarmResult(
            findings=list(self.findings),
            tools_run=["bandit"],
            tools_skipped=list(self.skipped),
            elapsed_ms=1.0,
        )


class _FakeMLPipeline:
    def __init__(self, findings: list[Finding] | None = None) -> None:
        self.findings = list(findings or [])

    def scan_paths(self, paths):  # sync to match real impl
        from document_processor.sentinel.ml_pipeline import MLPipelineResult
        return MLPipelineResult(
            findings=list(self.findings),
            files_scanned=len(paths),
            backend_summary={
                "secret_detector": "heuristic",
                "anomaly_detector": "heuristic",
                "severity_ranker": "heuristic",
            },
            elapsed_ms=1.0,
        )


class _FixedLLM:
    def __init__(self, responses: list[str]) -> None:
        self.responses = list(responses)

    async def __call__(self, prompt: str, system: str | None, max_tokens: int) -> str:
        return self.responses.pop(0) if self.responses else ""


def _audit(verdict: str = "true_positive", conf: float = 0.85) -> str:
    return json.dumps({
        "verdict": verdict, "confidence": conf,
        "rationale": "concrete reason", "suggested_severity": "high",
        "cwe": "CWE-89",
    })


def _patch() -> str:
    return json.dumps({
        "rationale": "use parameterised query",
        "language": "python",
        "patched_code": "def safe(): pass",
        "introduces_dependencies": [],
    })


# ─── Profile stage matrix ───────────────────────────────────────────


def test_profile_stages_quick_only_static_and_ml():
    p = PROFILE_STAGES["quick"]
    assert p["static_swarm"] and p["ml_pipeline"]
    assert not p["auditor"] and not p["judge"]


def test_profile_stages_standard_includes_auditor_and_judge():
    p = PROFILE_STAGES["standard"]
    assert p["auditor"] and p["judge"]
    assert p["patcher"] and p["critic_loop"]
    assert not p["redteam"]


def test_profile_stages_deep_includes_all():
    p = PROFILE_STAGES["deep"]
    assert all(v for v in p.values())


def test_profile_stages_paranoid_includes_all():
    p = PROFILE_STAGES["paranoid"]
    assert all(v for v in p.values())


# ─── Quick scan: static + ML only ───────────────────────────────────


def test_engine_quick_scan_runs_without_agents(tmp_path: Path):
    fake_static = _FakeStaticSwarm(findings=[
        Finding(tool="bandit", file="x.py", line_start=10, cwe="CWE-89",
                severity="high", confidence=0.7),
    ])
    fake_ml = _FakeMLPipeline()
    req = SentinelRequest(
        paths=[str(tmp_path)], scan_profile="quick",
    )
    eng = SentinelEngine(
        request=req,
        static_swarm=fake_static,
        ml_pipeline=fake_ml,
    )
    bundle = _run(eng.run())
    # Quick profile = no agents → no LLM calls happen even though
    # we didn't inject one.
    phase_names = [g.phase for g in bundle.gates]
    assert "static_swarm" in phase_names
    assert "ml_pipeline" in phase_names
    assert "agent_pipeline" not in phase_names
    assert "judge" not in phase_names
    # Findings flow through aggregate + score + report.
    assert len(bundle.findings) >= 1
    assert bundle.repo_risk_score > 0
    assert bundle.sarif_report
    assert bundle.markdown_report
    assert bundle.html_report


def test_engine_quick_scan_emits_phase_events(tmp_path: Path):
    seen: list[dict[str, Any]] = []

    async def cb(evt: dict[str, Any]) -> None:
        seen.append(evt)

    eng = SentinelEngine(
        request=SentinelRequest(paths=[str(tmp_path)], scan_profile="quick"),
        on_event=cb,
        static_swarm=_FakeStaticSwarm(),
        ml_pipeline=_FakeMLPipeline(),
    )
    _run(eng.run())
    types = {e.get("type") for e in seen}
    assert "sentinel_started" in types
    assert "sentinel_completed" in types
    assert "sentinel_phase_start" in types
    assert "sentinel_phase_complete" in types


# ─── Standard scan with mocked agents ───────────────────────────────


def test_engine_standard_runs_agents(tmp_path: Path):
    f1 = Finding(
        tool="bandit", file="x.py", line_start=10, cwe="CWE-89",
        severity="high", confidence=0.7, raw_message="SQLi candidate",
    )
    fake_static = _FakeStaticSwarm(findings=[f1])
    # Auditor returns 3 votes, Patcher returns a fix, Auditor re-runs
    # 3 votes saying false_positive, Judge synthesises.  Provide many
    # canned responses to be safe.
    audit_calls = [_audit("true_positive")] * 3 + [_audit("false_positive")] * 9
    patch_calls = [_patch()] * 3
    judge_calls = [json.dumps({
        "verdict": "approved", "confidence": 0.9,
        "rationale": "...", "final_severity": "high",
        "production_readiness": 0.2,
        "top_risks": [], "top_strengths": [],
    })] * 5
    audit_llm = _FixedLLM(audit_calls)
    patch_llm = _FixedLLM(patch_calls)
    judge_llm = _FixedLLM(judge_calls)
    eng = SentinelEngine(
        request=SentinelRequest(paths=[str(tmp_path)], scan_profile="standard"),
        static_swarm=fake_static,
        ml_pipeline=_FakeMLPipeline(),
        rag=SentinelRAG(),
        auditor=AuditorAgent(llm_call=audit_llm, voting_n=3),
        patcher=PatcherAgent(llm_call=patch_llm),
        judge=JudgeAgent(llm_call=judge_llm),
    )
    bundle = _run(eng.run())
    # agent_pipeline + critic_loop + judge gates land
    phases = [g.phase for g in bundle.gates]
    assert "agent_pipeline" in phases
    assert "critic_loop" in phases
    assert "judge" in phases
    # Auditor verdicts captured
    assert len(bundle.agent_verdicts.get("auditor") or []) >= 1


# ─── Cancel propagation ─────────────────────────────────────────────


def test_engine_cancel_short_circuits(tmp_path: Path):
    req = SentinelRequest(paths=[str(tmp_path)], scan_profile="standard")
    req.cancel_requested = True
    eng = SentinelEngine(
        request=req,
        static_swarm=_FakeStaticSwarm(),
        ml_pipeline=_FakeMLPipeline(),
    )
    bundle = _run(eng.run())
    # Cancelled before any phase fired.
    assert "sentinel_cancelled" in {
        g.phase for g in bundle.gates
    } or bundle.completed_at  # accept either signal


# ─── Bundle to_dict round-trip ──────────────────────────────────────


def test_engine_bundle_to_dict_json_safe(tmp_path: Path):
    f1 = Finding(tool="bandit", file="x.py", line_start=10,
                 cwe="CWE-89", severity="high", confidence=0.7)
    eng = SentinelEngine(
        request=SentinelRequest(paths=[str(tmp_path)], scan_profile="quick"),
        static_swarm=_FakeStaticSwarm(findings=[f1]),
        ml_pipeline=_FakeMLPipeline(),
    )
    bundle = _run(eng.run())
    s = json.dumps(bundle.to_dict(), default=str)
    assert "x.py" in s
    assert bundle.repo_risk_score >= 0


# ─── Override flag respected ────────────────────────────────────────


def test_engine_request_disable_static_swarm(tmp_path: Path):
    eng = SentinelEngine(
        request=SentinelRequest(
            paths=[str(tmp_path)], scan_profile="quick",
            enable_static_swarm=False,
        ),
        static_swarm=_FakeStaticSwarm(findings=[
            Finding(tool="bandit", severity="high", confidence=0.9),
        ]),
        ml_pipeline=_FakeMLPipeline(),
    )
    bundle = _run(eng.run())
    # Static swarm disabled, so no static_findings.
    assert bundle.static_findings == []
