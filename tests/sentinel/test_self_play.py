"""Unit tests for ``document_processor/sentinel/self_play.py``."""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest

from document_processor.sentinel.agents import (
    AuditorAgent,
    JudgeAgent,
    RedTeamAgent,
)
from document_processor.sentinel.models import (
    AgentVerdict,
    Finding,
    RAGContext,
)
from document_processor.sentinel.self_play import (
    DebateRunner,
    InjectionResult,
    SyntheticInjector,
)


def _run(coro):
    return asyncio.run(coro)


# ─── SyntheticInjector ──────────────────────────────────────────────


def test_injector_emits_sqli_into_function_body():
    clean = "def get_user(user_id):\n    return None\n"
    inj = SyntheticInjector()
    result = inj.inject(clean, recipe_index=0)
    assert result is not None
    assert result.cwe == "CWE-89"
    assert "SYNTHETIC-INJECT CWE-89" in result.injected_code
    assert "SELECT" in result.injected_code


def test_injector_returns_none_on_empty_code():
    assert SyntheticInjector().inject("") is None


def test_injector_returns_none_on_unmatched_pattern():
    # No def -> recipe 0 (which targets ^def) cannot inject.
    inj = SyntheticInjector()
    assert inj.inject("x = 1\n", recipe_index=0) is None


def test_injector_inject_all_returns_multiple():
    clean = (
        "import os\n"
        "def get_user(user_id):\n"
        "    return user_id\n"
    )
    out = SyntheticInjector().inject_all(clean)
    cwes = {r.cwe for r in out}
    # CWE-89 + CWE-94 + CWE-78 + CWE-502 all hook on a `def`.
    # CWE-798 hooks on `import` so it's also injectable.
    assert "CWE-89" in cwes
    assert "CWE-798" in cwes


def test_injector_evaluate_findings_match():
    inj = SyntheticInjector()
    clean = "def f():\n    return 1\n"
    res = inj.inject(clean, recipe_index=0)
    assert res is not None
    findings = [
        Finding(tool="bandit", cwe="CWE-89", file="x.py",
                line_start=res.line_inserted_after + 1),
    ]
    eval_ = SyntheticInjector.evaluate_findings(res, findings)
    assert eval_["found"] is True
    assert eval_["found_on_correct_line"] is True


def test_injector_evaluate_findings_miss():
    inj = SyntheticInjector()
    clean = "def f():\n    pass\n"
    res = inj.inject(clean, recipe_index=0)
    eval_ = SyntheticInjector.evaluate_findings(res, [])
    assert eval_["found"] is False
    assert eval_["match_count"] == 0


# ─── DebateRunner ───────────────────────────────────────────────────


class _FixedLLM:
    def __init__(self, responses: list[str]) -> None:
        self.responses = list(responses)

    async def __call__(self, prompt: str, system: str | None, max_tokens: int) -> str:
        return self.responses.pop(0) if self.responses else ""


def test_debate_skipped_when_verdicts_agree():
    a = AuditorAgent(llm_call=_FixedLLM([]))
    r = RedTeamAgent(llm_call=_FixedLLM([]))
    j = JudgeAgent(llm_call=_FixedLLM([]))
    runner = DebateRunner(auditor=a, redteam=r, judge=j, max_turns=5)
    f = Finding(tool="bandit", cwe="CWE-89")
    initial_aud = AgentVerdict(role="auditor", verdict="true_positive", confidence=0.8)
    initial_red = AgentVerdict(role="redteam", verdict="exploitable", confidence=0.85)
    res = _run(runner.run(
        finding=f, initial_auditor=initial_aud,
        initial_redteam=initial_red, context=RAGContext(),
    ))
    # No turns recorded — they already agreed.
    assert res.turns == []
    assert res.final_verdict == "true_positive"


def test_debate_runs_when_verdicts_disagree():
    audit_response = json.dumps({
        "verdict": "true_positive", "confidence": 0.7,
        "rationale": "still flagged",
        "suggested_severity": "high", "cwe": "CWE-89",
    })
    redteam_response = json.dumps({
        "verdict": "exploitable", "confidence": 0.85,
        "exploit_scenario": "concrete payload here",
        "preconditions": [], "impact_summary": "DB",
        "suggested_severity": "high",
    })
    judge_response = json.dumps({
        "verdict": "approved", "confidence": 0.9,
        "rationale": "auditor + redteam ultimately agree",
        "final_severity": "high", "production_readiness": 0.1,
        "top_risks": [], "top_strengths": [],
    })

    a = AuditorAgent(llm_call=_FixedLLM([audit_response] * 6), voting_n=1)
    r = RedTeamAgent(llm_call=_FixedLLM([redteam_response] * 6))
    j = JudgeAgent(llm_call=_FixedLLM([judge_response]))
    runner = DebateRunner(auditor=a, redteam=r, judge=j, max_turns=4)

    f = Finding(tool="bandit", cwe="CWE-89")
    initial_aud = AgentVerdict(role="auditor", verdict="true_positive", confidence=0.7)
    initial_red = AgentVerdict(role="redteam", verdict="not_exploitable", confidence=0.6)
    # Initial disagree → debate fires.
    res = _run(runner.run(
        finding=f, initial_auditor=initial_aud,
        initial_redteam=initial_red, context=RAGContext(),
    ))
    assert len(res.turns) >= 2
    assert res.final_verdict == "approved"


def test_debate_max_turns_clamped():
    a = AuditorAgent(llm_call=_FixedLLM([]))
    r = RedTeamAgent(llm_call=_FixedLLM([]))
    j = JudgeAgent(llm_call=_FixedLLM([]))
    runner = DebateRunner(auditor=a, redteam=r, judge=j, max_turns=99)
    assert runner.max_turns == 7
