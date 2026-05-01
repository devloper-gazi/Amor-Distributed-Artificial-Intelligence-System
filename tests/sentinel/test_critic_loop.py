"""Unit tests for ``document_processor/sentinel/critic_loop.py``."""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest

from document_processor.sentinel.agents import AuditorAgent, PatcherAgent
from document_processor.sentinel.critic_loop import CriticLoop, CriticResult
from document_processor.sentinel.models import Finding, RAGContext


def _run(coro):
    return asyncio.run(coro)


class _ScriptedLLM:
    def __init__(self, responses: list[str]) -> None:
        self.responses = list(responses)
        self.calls = 0

    async def __call__(self, prompt: str, system: str | None, max_tokens: int) -> str:
        self.calls += 1
        if not self.responses:
            return ""
        return self.responses.pop(0)


def _patch_response(code: str = "def safe(): pass", rationale: str = "param query") -> str:
    return json.dumps({
        "rationale": rationale,
        "language": "python",
        "patched_code": code,
        "introduces_dependencies": [],
    })


def _audit_response(verdict: str, conf: float = 0.7) -> str:
    return json.dumps({
        "verdict": verdict, "confidence": conf,
        "rationale": "...", "suggested_severity": "low", "cwe": "CWE-89",
    })


# ─── max_iters=0 short-circuits ─────────────────────────────────────


def test_critic_max_iters_zero_returns_immediately():
    p = PatcherAgent(llm_call=_ScriptedLLM([]))
    a = AuditorAgent(llm_call=_ScriptedLLM([]))
    loop = CriticLoop(patcher=p, auditor=a, max_iters=0)
    f = Finding(tool="bandit", cwe="CWE-89")
    res = _run(loop.refine(
        finding=f, auditor_summary="...", redteam_summary="",
        code_excerpt="def vuln(): pass", context=RAGContext(),
    ))
    assert res.iterations == 0
    assert res.converged is False


# ─── Converges on first patch ───────────────────────────────────────


def test_critic_converges_after_one_iter():
    # Patcher returns a fix; re-auditor (3 votes) all say false_positive.
    patcher_llm = _ScriptedLLM([_patch_response()])
    audit_llm = _ScriptedLLM([_audit_response("false_positive")] * 3)
    p = PatcherAgent(llm_call=patcher_llm)
    a = AuditorAgent(llm_call=audit_llm, voting_n=3)
    loop = CriticLoop(patcher=p, auditor=a, max_iters=3)
    f = Finding(tool="bandit", cwe="CWE-89")
    res = _run(loop.refine(
        finding=f, auditor_summary="vuln visible",
        redteam_summary="exploit X", code_excerpt="def vuln(): ...",
        context=RAGContext(),
    ))
    assert res.iterations == 1
    assert res.converged is True
    assert "def safe" in res.final_patched_code


# ─── Exhausts iterations ────────────────────────────────────────────


def test_critic_caps_at_max_iters():
    # Patcher always returns code; auditor always says true_positive.
    patcher_llm = _ScriptedLLM([_patch_response("def still_vuln(): pass")] * 4)
    audit_llm = _ScriptedLLM([_audit_response("true_positive")] * 12)
    p = PatcherAgent(llm_call=patcher_llm)
    a = AuditorAgent(llm_call=audit_llm, voting_n=3)
    loop = CriticLoop(patcher=p, auditor=a, max_iters=3)
    f = Finding(tool="bandit", cwe="CWE-89")
    res = _run(loop.refine(
        finding=f, auditor_summary="...", redteam_summary="",
        code_excerpt="def vuln(): ...", context=RAGContext(),
    ))
    assert res.iterations == 3
    assert res.converged is False
    # Should have logged 3 patch + 3 reaudit history entries.
    stages = [h.get("stage") for h in res.history]
    assert stages.count("patch") == 3
    assert stages.count("reaudit") == 3


# ─── Patcher gives up (no fix) → bail early ─────────────────────────


def test_critic_stops_when_patcher_returns_empty():
    # Patcher returns no patched_code; loop exits.
    patcher_llm = _ScriptedLLM(['{"rationale": "I cannot fix"}'])
    audit_llm = _ScriptedLLM([])
    p = PatcherAgent(llm_call=patcher_llm)
    a = AuditorAgent(llm_call=audit_llm, voting_n=1)
    loop = CriticLoop(patcher=p, auditor=a, max_iters=3)
    f = Finding(tool="bandit", cwe="CWE-89")
    res = _run(loop.refine(
        finding=f, auditor_summary="...", redteam_summary="",
        code_excerpt="x = 1", context=RAGContext(),
    ))
    assert res.iterations == 0
    assert res.converged is False


# ─── max_iters clamping ─────────────────────────────────────────────


def test_critic_max_iters_clamped_to_five():
    p = PatcherAgent(llm_call=_ScriptedLLM([]))
    a = AuditorAgent(llm_call=_ScriptedLLM([]))
    loop = CriticLoop(patcher=p, auditor=a, max_iters=99)
    assert loop.max_iters == 5
