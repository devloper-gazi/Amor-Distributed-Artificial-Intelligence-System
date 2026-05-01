"""Unit tests for ``document_processor/sentinel/agents.py``."""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest

from document_processor.sentinel.agents import (
    AuditorAgent,
    JudgeAgent,
    PatcherAgent,
    ReasonerAgent,
    RedTeamAgent,
    _parse_agent_json,
)
from document_processor.sentinel.models import Finding, RAGContext


def _run(coro):
    return asyncio.run(coro)


class _FixedLLM:
    """Async callable returning canned responses."""

    def __init__(self, responses: list[str]) -> None:
        self.responses = list(responses)
        self.calls: list[tuple[str, str | None, int]] = []

    async def __call__(self, prompt: str, system: str | None, max_tokens: int) -> str:
        self.calls.append((prompt, system, max_tokens))
        if not self.responses:
            return ""
        return self.responses.pop(0)


# ─── JSON parser ─────────────────────────────────────────────────────


def test_parse_agent_json_strips_fences():
    text = '```json\n{"verdict": "true_positive"}\n```'
    out = _parse_agent_json(text)
    assert out == {"verdict": "true_positive"}


def test_parse_agent_json_extracts_first_object():
    text = 'Here is the response: {"a": 1, "b": 2}\nThanks!'
    out = _parse_agent_json(text)
    assert out == {"a": 1, "b": 2}


def test_parse_agent_json_returns_none_on_garbage():
    assert _parse_agent_json("nothing here") is None
    assert _parse_agent_json("") is None


# ─── AuditorAgent ────────────────────────────────────────────────────


def _audit_response(verdict: str = "true_positive", conf: float = 0.85) -> str:
    return json.dumps({
        "verdict": verdict,
        "confidence": conf,
        "rationale": "user input flows directly to SQL string concat",
        "suggested_severity": "high",
        "cwe": "CWE-89",
    })


def test_auditor_returns_n_verdicts():
    llm = _FixedLLM([_audit_response()] * 3)
    agent = AuditorAgent(llm_call=llm, voting_n=3)
    f = Finding(tool="bandit", cwe="CWE-89", file="x.py", line_start=10)
    verdicts = _run(agent.audit(f, RAGContext()))
    assert len(verdicts) == 3
    assert all(v.verdict == "true_positive" for v in verdicts)


def test_auditor_majority_verdict_picks_modal():
    verdicts = [
        AuditorAgent._parse_static("dummy", "true_positive", 0.7),
        AuditorAgent._parse_static("dummy", "true_positive", 0.8),
        AuditorAgent._parse_static("dummy", "false_positive", 0.6),
    ] if False else None  # placeholder
    # Use the actual API instead.
    from document_processor.sentinel.models import AgentVerdict
    a = AgentVerdict(role="auditor", verdict="true_positive", confidence=0.7)
    b = AgentVerdict(role="auditor", verdict="true_positive", confidence=0.9)
    c = AgentVerdict(role="auditor", verdict="false_positive", confidence=0.4)
    winner = AuditorAgent.majority_verdict([a, b, c])
    assert winner.verdict == "true_positive"
    # Merged confidence = mean of agreeing voters
    assert winner.confidence == pytest.approx(0.8)


def test_auditor_majority_empty_returns_default():
    winner = AuditorAgent.majority_verdict([])
    assert winner.verdict == "needs_more_context"


def test_auditor_unparseable_returns_needs_more_context():
    llm = _FixedLLM(["this is not JSON"] * 3)
    agent = AuditorAgent(llm_call=llm, voting_n=3)
    f = Finding(tool="bandit", cwe="CWE-89")
    verdicts = _run(agent.audit(f, RAGContext()))
    assert all(v.verdict == "needs_more_context" for v in verdicts)
    assert all(v.confidence == 0.0 for v in verdicts)


# ─── ReasonerAgent ───────────────────────────────────────────────────


def test_reasoner_parses_cot():
    response = json.dumps({
        "verdict": "true_positive",
        "confidence": 0.78,
        "rationale": "input → cursor.execute() string concat → exploit",
        "exploit_steps": ["s1", "s2"],
        "suggested_severity": "high",
    })
    llm = _FixedLLM([response])
    agent = ReasonerAgent(llm_call=llm)
    f = Finding(tool="bandit", cwe="CWE-89")
    out = _run(agent.analyse(f, "summary", RAGContext()))
    assert out.role == "reasoner"
    assert out.verdict == "true_positive"
    assert out.suggested_severity == "high"


# ─── RedTeamAgent ────────────────────────────────────────────────────


def test_redteam_extracts_exploit_scenario():
    response = json.dumps({
        "verdict": "exploitable",
        "confidence": 0.9,
        "exploit_scenario": "send POST with id=1' OR 1=1-- ; observe full table dump",
        "preconditions": ["public endpoint"],
        "impact_summary": "DB exfiltration",
        "suggested_severity": "critical",
    })
    llm = _FixedLLM([response])
    agent = RedTeamAgent(llm_call=llm)
    f = Finding(tool="bandit", cwe="CWE-89")
    out = _run(agent.attack(f, RAGContext()))
    assert out.verdict == "exploitable"
    assert "id=1' OR 1=1" in out.exploit_scenario


# ─── PatcherAgent ────────────────────────────────────────────────────


def test_patcher_returns_fix_diff_when_provided():
    response = json.dumps({
        "rationale": "use parameterised query",
        "language": "python",
        "patched_code": "def get_user(id): return cursor.execute('SELECT * FROM u WHERE id=%s', (id,))",
        "introduces_dependencies": [],
    })
    llm = _FixedLLM([response])
    agent = PatcherAgent(llm_call=llm)
    f = Finding(tool="bandit", cwe="CWE-89")
    out = _run(agent.patch(f, "audit-summary", "redteam-summary", "<vuln code>"))
    assert out.verdict == "approved"
    assert "cursor.execute" in out.fix_diff


def test_patcher_no_code_returns_needs_more_context():
    llm = _FixedLLM(['{"rationale": "cannot fix without seeing more"}'])
    agent = PatcherAgent(llm_call=llm)
    f = Finding(tool="bandit", cwe="CWE-89")
    out = _run(agent.patch(f, "audit", "redteam", "<code>"))
    assert out.verdict == "needs_more_context"
    assert out.fix_diff == ""


# ─── JudgeAgent ──────────────────────────────────────────────────────


def test_judge_synthesizes_swarm_verdicts():
    response = json.dumps({
        "verdict": "approved",
        "confidence": 0.92,
        "rationale": "all three signals agree on SQLi",
        "final_severity": "critical",
        "production_readiness": 0.1,
        "top_risks": [{"title": "SQLi", "detail": "..."}],
        "top_strengths": [],
    })
    llm = _FixedLLM([response])
    agent = JudgeAgent(llm_call=llm)
    f = Finding(tool="bandit", cwe="CWE-89")
    out = _run(agent.synthesize(
        f,
        auditor_results=[
            {"verdict": "true_positive", "confidence": 0.8, "rationale": "..."},
            {"verdict": "true_positive", "confidence": 0.7, "rationale": "..."},
        ],
        reasoner_result={"verdict": "true_positive", "confidence": 0.78,
                         "rationale": "..."},
        redteam_result={"verdict": "exploitable", "confidence": 0.9,
                        "exploit_scenario": "..."},
    ))
    assert out.verdict == "approved"
    assert out.suggested_severity == "critical"


# ─── No raw_llm_output in to_dict ────────────────────────────────────


def test_agent_verdict_to_dict_drops_raw():
    llm = _FixedLLM([_audit_response()] * 3)
    agent = AuditorAgent(llm_call=llm, voting_n=1)
    f = Finding(tool="bandit", cwe="CWE-89")
    v = _run(agent.audit(f, RAGContext()))[0]
    d = v.to_dict()
    assert "raw_llm_output" not in d
    assert d["verdict"] == "true_positive"


# Helper for the parse-static tests (kept private, exposed as static
# would normally not exist; we sidestep above)
def _add_parse_static(cls):
    @staticmethod
    def parse_static(raw, verdict, conf):  # pragma: no cover
        from document_processor.sentinel.models import AgentVerdict
        return AgentVerdict(role="auditor", verdict=verdict, confidence=conf)
    cls._parse_static = parse_static
    return cls


_add_parse_static(AuditorAgent)
