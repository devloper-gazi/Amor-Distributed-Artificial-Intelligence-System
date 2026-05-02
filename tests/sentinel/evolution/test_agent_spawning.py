"""Unit tests for Subsystem E — dynamic agent spawning."""

from __future__ import annotations

import asyncio
import time
from pathlib import Path

import pytest

from document_processor.sentinel.evolution.agent_spawning import (
    AgentFactory,
    AgentPromoter,
    MetaMonitor,
    ShadowRunRecord,
    ShadowTracker,
    SpawnRecommendation,
    SpawnedAgent,
    derive_agent_name,
    derive_trigger_flag,
    slugify,
)
from document_processor.sentinel.evolution.governance import (
    LedgerStore,
    load_immutable_constraints,
)


def _run(coro):
    return asyncio.run(coro)


# ─── slugify / derive helpers ───────────────────────────────────────


def test_slugify_basic():
    assert slugify("Crypto Specialist!") == "crypto_specialist"
    assert slugify("") == "specialist"


def test_derive_agent_name_known_cwes():
    assert derive_agent_name("CWE-89") == "sqli_specialist"
    assert derive_agent_name("CWE-79") == "xss_specialist"
    assert derive_agent_name("CWE-327") == "crypto_specialist"
    assert derive_agent_name("CWE-9999").startswith("specialist_")


def test_derive_trigger_flag_includes_languages():
    flag = derive_trigger_flag("CWE-89", ["python", "go"])
    assert flag.startswith("sqli_relevant")
    assert "python" in flag and "go" in flag


# ─── MetaMonitor ────────────────────────────────────────────────────


def test_metamonitor_emits_recommendation_when_threshold_crossed():
    findings = [{"cwe": "CWE-89", "language": "python"} for _ in range(40)]
    findings += [{"cwe": "CWE-79", "language": "javascript"} for _ in range(60)]
    mon = MetaMonitor(window_size=100, threshold_percent=30.0)
    recs = mon.observe(findings)
    cwes = [r.cwe for r in recs]
    assert "CWE-79" in cwes
    assert "CWE-89" in cwes


def test_metamonitor_skips_below_threshold():
    findings = [{"cwe": "CWE-89", "language": "python"} for _ in range(20)]
    findings += [{"cwe": "CWE-79", "language": "javascript"} for _ in range(80)]
    mon = MetaMonitor(window_size=100, threshold_percent=30.0)
    recs = mon.observe(findings)
    assert all(r.cwe != "CWE-89" for r in recs)


def test_metamonitor_handles_empty_input():
    mon = MetaMonitor()
    assert mon.observe([]) == []


def test_metamonitor_window_size_truncates_history():
    findings = [{"cwe": "CWE-79", "language": "javascript"} for _ in range(50)]
    findings += [{"cwe": "CWE-89", "language": "python"} for _ in range(60)]
    mon = MetaMonitor(window_size=50, threshold_percent=50.0)
    # Only the last 50 are inspected → CWE-89 dominates.
    recs = mon.observe(findings)
    assert len(recs) == 1
    assert recs[0].cwe == "CWE-89"


# ─── AgentFactory.spawn ─────────────────────────────────────────────


class _FakeLLM:
    def __init__(self, response: str = "Specialist prompt: focus on crypto.") -> None:
        self.response = response

    async def __call__(self, prompt, system, max_tokens):
        return self.response


def test_spawn_writes_manifest_and_records_ledger(tmp_path: Path):
    ledger = LedgerStore(tmp_path)
    constraints = load_immutable_constraints()
    factory = AgentFactory(ledger=ledger, constraints=constraints, root=tmp_path)
    rec = SpawnRecommendation(
        cwe="CWE-327", occurrences=40, window_size=100,
        percent=40.0, languages=["python"],
        sample_findings=[{"file": "f.py", "line_start": 10,
                          "raw_message": "weak hash"}],
    )
    agent = _run(factory.spawn(
        rec,
        parent_system_prompt="You are an expert auditor.",
        cwe_corpus_entry={
            "name": "Use of a Broken or Risky Cryptographic Algorithm",
            "description": "MD5 is fast and finds collisions.",
            "mitigation": "Use Argon2 or bcrypt.",
        },
        llm=_FakeLLM("Crypto specialist: focus on weak hashes + IV reuse."),
    ))
    assert agent is not None
    assert agent.name == "crypto_specialist"
    assert "Crypto specialist" in agent.system_prompt
    # Manifest written to disk.
    manifest = tmp_path / "spawned_agents" / "crypto_specialist" / "manifest.yaml"
    assert manifest.is_file()
    # Ledger entry recorded.
    kinds = [e.kind for e in ledger.entries()]
    assert "agent_spawned" in kinds


def test_spawn_blocks_violating_prompt(tmp_path: Path):
    ledger = LedgerStore(tmp_path)
    constraints = load_immutable_constraints()
    factory = AgentFactory(ledger=ledger, constraints=constraints, root=tmp_path)
    rec = SpawnRecommendation(
        cwe="CWE-327", occurrences=40, window_size=100,
        percent=40.0, languages=["python"],
    )
    agent = _run(factory.spawn(
        rec,
        parent_system_prompt="You are an expert.",
        cwe_corpus_entry=None,
        # LLM returns a forbidden phrase.
        llm=_FakeLLM("Specialist: rm -rf / when in doubt."),
    ))
    assert agent is None
    kinds = [e.kind for e in ledger.entries()]
    assert "constraint_check_failed" in kinds


def test_factory_list_agents(tmp_path: Path):
    ledger = LedgerStore(tmp_path)
    constraints = load_immutable_constraints()
    factory = AgentFactory(ledger=ledger, constraints=constraints, root=tmp_path)
    agent = SpawnedAgent(
        name="xss_specialist", primary_cwe="CWE-79",
        languages=["javascript"], trigger_flag="xss_relevant_javascript",
        system_prompt="P", status="shadow", created_at=time.time(),
    )
    factory.write(agent)
    found = factory.list_agents()
    assert any(a.name == "xss_specialist" for a in found)
    # status filter
    shadow_only = factory.list_agents(status="shadow")
    assert all(a.status == "shadow" for a in shadow_only)
    assert factory.list_agents(status="active") == []


# ─── Shadow tracker + promoter ──────────────────────────────────────


def test_promoter_keeps_shadow_when_too_early(tmp_path: Path):
    ledger = LedgerStore(tmp_path)
    constraints = load_immutable_constraints()
    factory = AgentFactory(ledger=ledger, constraints=constraints, root=tmp_path)
    tracker = ShadowTracker(factory=factory)
    promoter = AgentPromoter(
        factory=factory, tracker=tracker, ledger=ledger,
        shadow_days=30,
    )
    agent = SpawnedAgent(
        name="sqli_specialist", primary_cwe="CWE-89",
        languages=["python"], trigger_flag="sqli_relevant_python",
        system_prompt="P", status="shadow",
        created_at=time.time(),  # just born
    )
    factory.write(agent)
    out = promoter.maybe_promote(agent)
    assert out.status == "shadow"


def test_promoter_promotes_after_shadow_period(tmp_path: Path):
    ledger = LedgerStore(tmp_path)
    constraints = load_immutable_constraints()
    factory = AgentFactory(ledger=ledger, constraints=constraints, root=tmp_path)
    tracker = ShadowTracker(factory=factory)
    promoter = AgentPromoter(
        factory=factory, tracker=tracker, ledger=ledger,
        shadow_days=30, improvement_percent=0.10,
    )
    agent = SpawnedAgent(
        name="sqli_specialist", primary_cwe="CWE-89",
        languages=["python"], trigger_flag="sqli_relevant_python",
        system_prompt="P", status="shadow",
        created_at=time.time() - 31 * 86400,  # 31 days ago
    )
    factory.write(agent)

    # Record shadow runs where the spawned agent beats parent.
    for i in range(10):
        tracker.record(agent.name, ShadowRunRecord(
            timestamp=time.time(),
            finding_fingerprint=f"f{i}",
            cwe="CWE-89",
            parent_verdict="false_positive",
            parent_confidence=0.5,
            spawned_verdict="true_positive",
            spawned_confidence=0.85,
            user_truth="true_positive",
        ))
    out = promoter.maybe_promote(agent)
    assert out.status == "active"
    kinds = [e.kind for e in ledger.entries()]
    assert "agent_promoted" in kinds


def test_promoter_demotes_to_dormant_when_no_improvement(tmp_path: Path):
    ledger = LedgerStore(tmp_path)
    constraints = load_immutable_constraints()
    factory = AgentFactory(ledger=ledger, constraints=constraints, root=tmp_path)
    tracker = ShadowTracker(factory=factory)
    promoter = AgentPromoter(
        factory=factory, tracker=tracker, ledger=ledger,
        shadow_days=30, improvement_percent=0.15,
    )
    agent = SpawnedAgent(
        name="path_specialist", primary_cwe="CWE-22",
        languages=["python"], trigger_flag="path_relevant",
        system_prompt="P", status="shadow",
        created_at=time.time() - 31 * 86400,
    )
    factory.write(agent)

    # Spawned matches parent — no delta.
    for i in range(8):
        tracker.record(agent.name, ShadowRunRecord(
            timestamp=time.time(), finding_fingerprint=f"f{i}", cwe="CWE-22",
            parent_verdict="true_positive", parent_confidence=0.7,
            spawned_verdict="true_positive", spawned_confidence=0.7,
            user_truth="true_positive",
        ))
    out = promoter.maybe_promote(agent)
    assert out.status == "dormant"
    kinds = [e.kind for e in ledger.entries()]
    assert "agent_archived" in kinds
