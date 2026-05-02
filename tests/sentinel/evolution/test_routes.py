"""Smoke tests for ``/api/sentinel/evolution/*`` routes.

These run against the real router with a temp evolution root, so
they exercise the constraint check + ledger append + dataclass
look-up paths end-to-end.  No Docker, no Ollama — pure FS.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Iterator

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from document_processor.api.sentinel_evolution_routes import router
from document_processor.config.settings import settings
from document_processor.sentinel.evolution.agent_spawning import (
    AgentFactory,
    SpawnedAgent,
)
from document_processor.sentinel.evolution.dag_mutation import (
    DAG,
    DAGStore,
    DEFAULT_DAG,
    parallelise,
)
from document_processor.sentinel.evolution.governance import (
    LedgerStore,
    load_immutable_constraints,
)
from document_processor.sentinel.evolution.lora_pipeline import (
    AdapterStore,
    AdapterVersion,
)
from document_processor.sentinel.evolution.prompt_evolution import (
    PromptStore,
    PromptVersion,
)
from document_processor.sentinel.evolution.rule_synthesis import (
    RuleStore,
    SynthesizedRule,
)


# ─── fixtures ───────────────────────────────────────────────────────


@pytest.fixture
def evolution_root(tmp_path: Path, monkeypatch) -> Iterator[Path]:
    """Point the route module at a clean tmp dir for the test."""
    root = tmp_path / "evolution"
    root.mkdir()
    monkeypatch.setattr(settings, "sentinel_evolution_root", str(root))
    monkeypatch.setattr(settings, "sentinel_evolution_enabled", True)
    yield root


@pytest.fixture
def client(evolution_root: Path) -> TestClient:
    app = FastAPI()
    app.include_router(router)
    return TestClient(app)


# ─── health ─────────────────────────────────────────────────────────


def test_health_returns_intact_chain(client: TestClient, evolution_root: Path):
    resp = client.get("/api/sentinel/evolution/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert body["enabled"] is True
    assert body["ledger_intact"] is True
    assert body["entry_count"] == 0


def test_health_when_disabled(client: TestClient, monkeypatch):
    monkeypatch.setattr(settings, "sentinel_evolution_enabled", False)
    resp = client.get("/api/sentinel/evolution/health")
    assert resp.status_code == 503


# ─── genome ─────────────────────────────────────────────────────────


def test_genome_empty_root(client: TestClient):
    resp = client.get("/api/sentinel/evolution/genome")
    assert resp.status_code == 200
    body = resp.json()
    assert "production" in body
    # Empty root → no DAG, no prompts, no agents.
    prod = body["production"]
    assert prod["agents"] == []
    assert prod["rules"] == []


def test_genome_reflects_default_dag(client: TestClient, evolution_root: Path):
    # Initialising a DAGStore against the architecture sub-dir
    # bootstraps the default DAG into production.
    store = DAGStore(evolution_root)
    assert store.get_production() is not None

    resp = client.get("/api/sentinel/evolution/genome")
    assert resp.status_code == 200
    prod = resp.json()["production"]
    assert prod["dag_version"] == DEFAULT_DAG.version
    assert prod["dag_node_count"] == len(DEFAULT_DAG.nodes)
    assert prod["dag_edge_count"] == len(DEFAULT_DAG.edges)


# ─── ledger ─────────────────────────────────────────────────────────


def test_ledger_initially_empty(client: TestClient):
    resp = client.get("/api/sentinel/evolution/ledger")
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 0
    assert body["entries"] == []
    assert body["intact"] is True


def test_ledger_filters_by_kind(client: TestClient, evolution_root: Path):
    store = LedgerStore(evolution_root)
    store.append(actor="test", kind="prompt_promoted", payload={"a": 1})
    store.append(actor="test", kind="dag_promoted", payload={"b": 2})
    store.append(actor="test", kind="prompt_promoted", payload={"a": 3})

    resp = client.get(
        "/api/sentinel/evolution/ledger",
        params={"kind": "prompt_promoted"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 2
    for e in body["entries"]:
        assert e["kind"] == "prompt_promoted"


def test_ledger_pagination(client: TestClient, evolution_root: Path):
    store = LedgerStore(evolution_root)
    for i in range(5):
        store.append(actor="test", kind="manual_trigger", payload={"i": i})

    resp = client.get(
        "/api/sentinel/evolution/ledger",
        params={"limit": 2, "offset": 1},
    )
    body = resp.json()
    assert body["total"] == 5
    assert body["returned"] == 2


# ─── proposals ──────────────────────────────────────────────────────


def test_proposals_empty(client: TestClient):
    resp = client.get("/api/sentinel/evolution/proposals")
    assert resp.status_code == 200
    body = resp.json()
    assert body["proposals"] == []


def test_proposals_picks_up_staging_rules(
    client: TestClient, evolution_root: Path,
):
    rstore = RuleStore(evolution_root)
    rule = SynthesizedRule(
        rule_id="custom-cwe89-001",
        cwe="CWE-89",
        language="python",
        yaml="rules: []",
        source_examples=[],
        status="staging",
    )
    rstore.write(rule)

    resp = client.get("/api/sentinel/evolution/proposals")
    body = resp.json()
    assert any(p["kind"] == "rule" and p["id"] == "custom-cwe89-001"
               for p in body["proposals"])


def test_proposals_picks_up_staging_prompts(
    client: TestClient, evolution_root: Path,
):
    ps = PromptStore(evolution_root)
    pv = PromptVersion(
        agent_name="auditor",
        version="v002",
        system_prompt="You are an auditor.",
        parent_version="v001",
        mutation_method="genetic",
        few_shot_demos=[],
        eval_metrics={"precision": 0.7, "recall": 0.6},
        status="staging",
        created_at=time.time(),
    )
    ps.write_version(pv)

    resp = client.get("/api/sentinel/evolution/proposals")
    body = resp.json()
    found = [p for p in body["proposals"]
             if p["kind"] == "prompt" and p["id"] == "auditor/v002"]
    assert found
    assert found[0]["agent_or_label"] == "auditor"
    assert found[0]["metrics"]["precision"] == 0.7


def test_proposals_picks_up_shadow_agents(
    client: TestClient, evolution_root: Path,
):
    ledger = LedgerStore(evolution_root)
    constraints = load_immutable_constraints()
    factory = AgentFactory(
        ledger=ledger, constraints=constraints, root=evolution_root,
    )
    spawned = SpawnedAgent(
        name="sqli_specialist",
        primary_cwe="CWE-89",
        languages=["python"],
        trigger_flag="sqli_relevant_python",
        system_prompt="You are an SQL injection specialist.",
        status="shadow",
        created_at=time.time(),
    )
    factory.write(spawned)

    resp = client.get("/api/sentinel/evolution/proposals")
    body = resp.json()
    found = [p for p in body["proposals"]
             if p["kind"] == "agent" and p["id"] == "sqli_specialist"]
    assert found


# ─── stats ──────────────────────────────────────────────────────────


def test_stats_empty(client: TestClient):
    resp = client.get("/api/sentinel/evolution/stats")
    assert resp.status_code == 200
    body = resp.json()
    assert body["ledger_entries"] == 0
    assert body["ledger_intact"] is True
    assert "counts" in body


def test_stats_after_writes(client: TestClient, evolution_root: Path):
    rstore = RuleStore(evolution_root)
    rstore.write(SynthesizedRule(
        rule_id="r1", cwe="CWE-89", language="python",
        yaml="rules: []", source_examples=[], status="staging",
    ))
    rstore.write(SynthesizedRule(
        rule_id="r2", cwe="CWE-79", language="javascript",
        yaml="rules: []", source_examples=[], status="staging",
    ))
    resp = client.get("/api/sentinel/evolution/stats")
    body = resp.json()
    assert body["counts"]["rules"]["staging"] == 2


# ─── promote — happy paths ──────────────────────────────────────────


def test_promote_prompt(client: TestClient, evolution_root: Path):
    ps = PromptStore(evolution_root)
    pv = PromptVersion(
        agent_name="auditor", version="v002",
        system_prompt="You are an auditor.",
        parent_version="v001", mutation_method="genetic",
        few_shot_demos=[],
        eval_metrics={"precision": 0.8, "recall": 0.6},
        status="staging",
    )
    ps.write_version(pv)

    resp = client.post(
        "/api/sentinel/evolution/promote",
        json={"kind": "prompt", "target_id": "auditor/v002",
              "note": "smoke test"},
        headers={"X-Client-Id": "tester"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert body["ledger_entry_id"]

    # Ledger was updated.
    ledger_resp = client.get("/api/sentinel/evolution/ledger")
    kinds = [e["kind"] for e in ledger_resp.json()["entries"]]
    assert "prompt_promoted" in kinds


def test_promote_rule(client: TestClient, evolution_root: Path):
    rstore = RuleStore(evolution_root)
    rule = SynthesizedRule(
        rule_id="custom-cwe89-001", cwe="CWE-89", language="python",
        yaml="rules: []", source_examples=[], status="staging",
    )
    rstore.write(rule)

    resp = client.post(
        "/api/sentinel/evolution/promote",
        json={"kind": "rule", "target_id": "custom-cwe89-001"},
        headers={"X-Client-Id": "tester"},
    )
    assert resp.status_code == 200
    assert resp.json()["ok"] is True


def test_promote_dag(client: TestClient, evolution_root: Path):
    store = DAGStore(evolution_root)
    candidate = parallelise(DEFAULT_DAG, "auditor", 5)
    assert candidate is not None
    store.write(candidate, status="staging")

    resp = client.post(
        "/api/sentinel/evolution/promote",
        json={"kind": "dag", "target_id": candidate.version},
        headers={"X-Client-Id": "tester"},
    )
    assert resp.status_code == 200
    assert resp.json()["ok"] is True
    assert store.get_production().version == candidate.version


def test_promote_agent(client: TestClient, evolution_root: Path):
    ledger = LedgerStore(evolution_root)
    constraints = load_immutable_constraints()
    factory = AgentFactory(
        ledger=ledger, constraints=constraints, root=evolution_root,
    )
    spawned = SpawnedAgent(
        name="sqli_specialist", primary_cwe="CWE-89",
        languages=["python"], trigger_flag="sqli_relevant_python",
        system_prompt="You are an SQL injection specialist.",
        status="shadow", created_at=time.time(),
    )
    factory.write(spawned)

    resp = client.post(
        "/api/sentinel/evolution/promote",
        json={"kind": "agent", "target_id": "sqli_specialist"},
        headers={"X-Client-Id": "tester"},
    )
    assert resp.status_code == 200
    # Re-load — status should now be 'active'.
    promoted = next(a for a in factory.list_agents() if a.name == "sqli_specialist")
    assert promoted.status == "active"


# ─── promote — failure paths ────────────────────────────────────────


def test_promote_unknown_id_returns_404(client: TestClient):
    resp = client.post(
        "/api/sentinel/evolution/promote",
        json={"kind": "prompt", "target_id": "ghost/v999"},
        headers={"X-Client-Id": "tester"},
    )
    assert resp.status_code == 404


def test_promote_malformed_target_returns_400(client: TestClient):
    resp = client.post(
        "/api/sentinel/evolution/promote",
        json={"kind": "prompt", "target_id": "no-slash-here"},
        headers={"X-Client-Id": "tester"},
    )
    assert resp.status_code == 400


def test_promote_constraint_violation_returns_400(
    client: TestClient, evolution_root: Path,
):
    # Inject a rule with a forbidden phrase in the payload note.
    rstore = RuleStore(evolution_root)
    rstore.write(SynthesizedRule(
        rule_id="bad-rule", cwe="CWE-89", language="python",
        yaml="rules: []", source_examples=[], status="staging",
    ))
    resp = client.post(
        "/api/sentinel/evolution/promote",
        json={"kind": "rule", "target_id": "bad-rule",
              "note": "rm -rf / it all"},
        headers={"X-Client-Id": "tester"},
    )
    assert resp.status_code == 400
    # constraint_check_failed got recorded.
    ledger_resp = client.get("/api/sentinel/evolution/ledger")
    kinds = [e["kind"] for e in ledger_resp.json()["entries"]]
    assert "constraint_check_failed" in kinds


# ─── trigger ────────────────────────────────────────────────────────


def test_trigger_records_ledger_when_allowed(
    client: TestClient, monkeypatch,
):
    monkeypatch.setattr(settings, "sentinel_evolution_allow_prompt_trigger", True)
    resp = client.post(
        "/api/sentinel/evolution/trigger/prompt",
        json={"payload": {"agent": "auditor", "n_mutants": 5},
              "note": "smoke"},
        headers={"X-Client-Id": "tester"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert body["subsystem"] == "prompt"
    assert body["ledger_entry_id"]


def test_trigger_blocks_when_subsystem_disabled(
    client: TestClient, monkeypatch,
):
    monkeypatch.setattr(settings, "sentinel_evolution_allow_lora_trigger", False)
    resp = client.post(
        "/api/sentinel/evolution/trigger/lora",
        json={"payload": {}},
        headers={"X-Client-Id": "tester"},
    )
    assert resp.status_code == 403


def test_trigger_unknown_subsystem(client: TestClient):
    resp = client.post(
        "/api/sentinel/evolution/trigger/banana",
        json={"payload": {}},
        headers={"X-Client-Id": "tester"},
    )
    assert resp.status_code == 400


# ─── rollback ───────────────────────────────────────────────────────


def test_rollback_dag(client: TestClient, evolution_root: Path):
    store = DAGStore(evolution_root)
    # The default DAG is already production.
    candidate = parallelise(DEFAULT_DAG, "auditor", 5)
    assert candidate is not None
    store.write(candidate, status="staging")
    store.promote(candidate)
    assert store.get_production().version == candidate.version

    # Roll back to default.
    resp = client.post(
        "/api/sentinel/evolution/rollback",
        json={"kind": "dag", "agent_or_label": "pipeline",
              "target_version": DEFAULT_DAG.version,
              "note": "revert"},
        headers={"X-Client-Id": "tester"},
    )
    assert resp.status_code == 200
    assert store.get_production().version == DEFAULT_DAG.version
    ledger_resp = client.get("/api/sentinel/evolution/ledger")
    kinds = [e["kind"] for e in ledger_resp.json()["entries"]]
    assert "dag_rolled_back" in kinds


def test_rollback_unknown_dag_version_returns_404(client: TestClient):
    resp = client.post(
        "/api/sentinel/evolution/rollback",
        json={"kind": "dag", "agent_or_label": "pipeline",
              "target_version": "ghost-version"},
        headers={"X-Client-Id": "tester"},
    )
    assert resp.status_code == 404
