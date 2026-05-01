"""Unit tests for Subsystems B (LoRA orchestrator) and F
(distillation orchestrator).  All tests run with the stub
backend so no GPU / heavy ML deps are needed."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from document_processor.sentinel.evolution.distillation import (
    DistillationCorpus,
    DistillationOrchestrator,
    EasyCaseRouter,
    RoutingFeatures,
    StudentConfig,
    StudentManifest,
    StudentStore,
    detect_distillation_backend,
)
from document_processor.sentinel.evolution.governance import (
    LedgerStore,
    load_immutable_constraints,
)
from document_processor.sentinel.evolution.lora_pipeline import (
    AdapterStore,
    AdapterVersion,
    EvalResult,
    HoldoutCase,
    LoRAOrchestrator,
    TrainConfig,
    detect_lora_backend,
    evaluate_adapter,
)


def _run(coro):
    return asyncio.run(coro)


# ─── Backend detection (stub when heavy deps absent) ────────────────


def test_lora_backend_defaults_to_stub_when_peft_absent():
    # On the test host (no peft / unsloth / trl installed) we expect
    # the stub backend.  This is a smoke test — if the host happens
    # to have the deps, accept those values too.
    backend = detect_lora_backend()
    assert backend in ("stub", "peft", "unsloth")


def test_distillation_backend_defaults_to_stub():
    backend = detect_distillation_backend()
    assert backend in ("stub", "peft", "unsloth")


# ─── AdapterStore round-trip ────────────────────────────────────────


def test_adapter_store_promote_demotes_previous(tmp_path: Path):
    store = AdapterStore(tmp_path)
    v1 = AdapterVersion(
        agent_name="auditor", version="v001",
        base_model="qwen2.5-coder:7b", method="dpo",
        backend="stub", artifact_path="/tmp/v1.bin",
        status="production", created_at=1.0,
    )
    v2 = AdapterVersion(
        agent_name="auditor", version="v002",
        base_model="qwen2.5-coder:7b", method="dpo",
        backend="stub", artifact_path="/tmp/v2.bin",
        status="staging", created_at=2.0,
    )
    store.write(v1)
    store.write(v2)
    store.promote(v2)
    out = {v.version: v.status for v in store.list_versions("auditor")}
    assert out["v001"] == "archived"
    assert out["v002"] == "production"


def test_adapter_store_rollback_to_old_version(tmp_path: Path):
    store = AdapterStore(tmp_path)
    v1 = AdapterVersion(
        agent_name="auditor", version="v001",
        base_model="qwen2.5-coder:7b", backend="stub",
        artifact_path="/tmp/v1.bin", status="archived",
    )
    v2 = AdapterVersion(
        agent_name="auditor", version="v002",
        base_model="qwen2.5-coder:7b", backend="stub",
        artifact_path="/tmp/v2.bin", status="production",
    )
    store.write(v1)
    store.write(v2)
    out = store.rollback_to("auditor", "v001")
    assert out is not None
    assert out.version == "v001"
    assert out.status == "production"
    statuses = {v.version: v.status for v in store.list_versions("auditor")}
    assert statuses["v002"] == "archived"


def test_adapter_store_rollback_unknown_version_returns_none(tmp_path: Path):
    store = AdapterStore(tmp_path)
    assert store.rollback_to("auditor", "does-not-exist") is None


# ─── evaluate_adapter ───────────────────────────────────────────────


def test_evaluate_adapter_perfect_score():
    cases = [
        HoldoutCase(user_prompt="x", expected_verdict="true_positive"),
        HoldoutCase(user_prompt="y", expected_verdict="true_positive"),
    ]

    async def scorer(adapter, sys_p, user_p, max_t):
        return json.dumps({"verdict": "true_positive"})

    res = _run(evaluate_adapter(
        adapter_path="/tmp/x", system_prompt="P",
        cases=cases, scorer=scorer,
    ))
    assert res.precision == 1.0
    assert res.recall == 1.0


def test_evaluate_adapter_handles_garbage_output():
    cases = [HoldoutCase(user_prompt="x", expected_verdict="false_positive")]

    async def scorer(adapter, sys_p, user_p, max_t):
        return "not parseable"

    res = _run(evaluate_adapter(
        adapter_path="/tmp/x", system_prompt="P",
        cases=cases, scorer=scorer,
    ))
    assert res.cases == 1
    # No positive predictions → precision 0.
    assert res.precision == 0.0


# ─── LoRAOrchestrator end-to-end (stub backend) ─────────────────────


def test_orchestrator_promotes_when_pareto_improving(tmp_path: Path):
    ledger = LedgerStore(tmp_path)
    constraints = load_immutable_constraints()
    store = AdapterStore(tmp_path)

    # Seed a baseline.
    baseline = AdapterVersion(
        agent_name="auditor", version="v001",
        base_model="qwen2.5-coder:7b", backend="stub",
        artifact_path=str(tmp_path / "v001.stub"),
        status="production",
    )
    store.write(baseline)
    Path(baseline.artifact_path).write_text("v001-stub", encoding="utf-8")
    baseline_metrics = EvalResult(precision=0.6, recall=0.5)

    # Scorer that returns a perfect verdict for the new adapter.
    async def scorer(adapter, sys_p, user_p, max_t):
        return json.dumps({"verdict": "true_positive"})

    holdout = [
        HoldoutCase(user_prompt=f"c{i}", expected_verdict="true_positive")
        for i in range(5)
    ]

    orch = LoRAOrchestrator(
        store=store, ledger=ledger, constraints=constraints,
        sandbox_root=tmp_path,
    )
    out = _run(orch.train_and_evaluate(
        agent_name="auditor",
        parent_version="v001",
        preferences_path=tmp_path / "prefs.jsonl",
        baseline_version=baseline,
        baseline_metrics=baseline_metrics,
        holdout=holdout,
        scorer=scorer,
        system_prompt="You are an expert auditor.",
        config=TrainConfig(),
    ))
    assert out.status == "production"
    kinds = [e.kind for e in ledger.entries()]
    assert "lora_promoted" in kinds


def test_orchestrator_rejects_below_floor(tmp_path: Path):
    ledger = LedgerStore(tmp_path)
    constraints = load_immutable_constraints()
    store = AdapterStore(tmp_path)
    baseline = AdapterVersion(
        agent_name="auditor", version="v001",
        base_model="qwen2.5-coder:7b", backend="stub",
        artifact_path=str(tmp_path / "v001.stub"),
        status="production",
    )
    store.write(baseline)
    Path(baseline.artifact_path).write_text("v001", encoding="utf-8")

    # Scorer always returns false_positive — recall 0 — below floor.
    async def scorer(adapter, sys_p, user_p, max_t):
        return json.dumps({"verdict": "false_positive"})

    holdout = [
        HoldoutCase(user_prompt=f"c{i}", expected_verdict="true_positive")
        for i in range(5)
    ]
    orch = LoRAOrchestrator(
        store=store, ledger=ledger, constraints=constraints,
        sandbox_root=tmp_path,
    )
    out = _run(orch.train_and_evaluate(
        agent_name="auditor",
        parent_version="v001",
        preferences_path=tmp_path / "prefs.jsonl",
        baseline_version=baseline,
        baseline_metrics=EvalResult(precision=0.5, recall=0.5),
        holdout=holdout,
        scorer=scorer,
        system_prompt="P",
    ))
    assert out.status == "rejected"
    kinds = [e.kind for e in ledger.entries()]
    assert "lora_trained" in kinds
    assert "lora_promoted" not in kinds


def test_orchestrator_rollback_records_ledger(tmp_path: Path):
    ledger = LedgerStore(tmp_path)
    constraints = load_immutable_constraints()
    store = AdapterStore(tmp_path)

    v1 = AdapterVersion(
        agent_name="auditor", version="v001",
        base_model="qwen2.5-coder:7b", backend="stub",
        artifact_path=str(tmp_path / "v001.stub"), status="archived",
    )
    v2 = AdapterVersion(
        agent_name="auditor", version="v002",
        base_model="qwen2.5-coder:7b", backend="stub",
        artifact_path=str(tmp_path / "v002.stub"), status="production",
    )
    store.write(v1)
    store.write(v2)

    orch = LoRAOrchestrator(
        store=store, ledger=ledger, constraints=constraints,
        sandbox_root=tmp_path,
    )
    out = orch.rollback(agent_name="auditor", version="v001")
    assert out is not None and out.version == "v001"
    kinds = [e.kind for e in ledger.entries()]
    assert "lora_rolled_back" in kinds


# ─── DistillationCorpus ─────────────────────────────────────────────


def test_distillation_corpus_append_and_count(tmp_path: Path):
    corp = DistillationCorpus(tmp_path)
    corp.append(teacher_input="case 1", teacher_output="approve")
    corp.append(teacher_input="case 2", teacher_output="reject",
                teacher_confidence=0.9)
    assert corp.count() == 2


def test_distillation_corpus_sft_export(tmp_path: Path):
    corp = DistillationCorpus(tmp_path)
    for i in range(3):
        corp.append(teacher_input=f"in {i}", teacher_output=f"out {i}")
    out = tmp_path / "sft.jsonl"
    rows = corp.export_sft_dataset(out)
    assert rows == 3
    text = out.read_text(encoding="utf-8")
    for i in range(3):
        assert f"in {i}" in text


# ─── EasyCaseRouter ─────────────────────────────────────────────────


def test_router_routes_easy_to_fast():
    r = EasyCaseRouter()
    decision = r.decide(RoutingFeatures(
        agent_vote_variance=0.05, cwe_rarity=0.1,
        file_complexity=0.3, confidence=0.85,
    ))
    assert decision.route == "fast"


def test_router_routes_high_variance_to_full():
    r = EasyCaseRouter()
    decision = r.decide(RoutingFeatures(
        agent_vote_variance=0.4, cwe_rarity=0.1,
        file_complexity=0.3, confidence=0.85,
    ))
    assert decision.route == "full"


def test_router_routes_rare_cwe_to_full():
    r = EasyCaseRouter()
    decision = r.decide(RoutingFeatures(
        agent_vote_variance=0.05, cwe_rarity=0.9,
        file_complexity=0.2, confidence=0.85,
    ))
    assert decision.route == "full"


def test_router_routes_low_confidence_to_full():
    r = EasyCaseRouter()
    decision = r.decide(RoutingFeatures(
        agent_vote_variance=0.05, cwe_rarity=0.1,
        file_complexity=0.2, confidence=0.4,
    ))
    assert decision.route == "full"


# ─── DistillationOrchestrator ───────────────────────────────────────


def test_distill_orchestrator_ready_when_threshold_crossed(tmp_path: Path):
    corp = DistillationCorpus(tmp_path)
    store = StudentStore(tmp_path)
    ledger = LedgerStore(tmp_path)
    constraints = load_immutable_constraints()
    orch = DistillationOrchestrator(
        corpus=corp, store=store, ledger=ledger,
        constraints=constraints, sandbox_root=tmp_path,
        trigger_rows=3,
    )
    assert orch.ready_to_train() is False
    for i in range(3):
        corp.append(teacher_input=f"in{i}", teacher_output=f"out{i}")
    assert orch.ready_to_train() is True


def test_distill_orchestrator_train_writes_manifest(tmp_path: Path):
    corp = DistillationCorpus(tmp_path)
    store = StudentStore(tmp_path)
    ledger = LedgerStore(tmp_path)
    constraints = load_immutable_constraints()
    orch = DistillationOrchestrator(
        corpus=corp, store=store, ledger=ledger,
        constraints=constraints, sandbox_root=tmp_path,
    )
    for i in range(5):
        corp.append(teacher_input=f"in{i}", teacher_output=f"out{i}")
    manifest = _run(orch.train(
        teacher="judge", name="FastJudge",
        config=StudentConfig(student_base="phi-3.5-mini"),
    ))
    assert manifest.status in ("staging", "production")
    assert manifest.rows_used == 5
    assert Path(manifest.artifact_path).exists()
    kinds = [e.kind for e in ledger.entries()]
    assert "distillation_trained" in kinds


def test_student_store_promote_replaces_production(tmp_path: Path):
    store = StudentStore(tmp_path)
    a = StudentManifest(
        name="FastJudge_v1", teacher="judge",
        student_base="phi-3.5-mini", backend="stub",
        artifact_path=str(tmp_path / "a.bin"), rows_used=10,
        status="production",
    )
    b = StudentManifest(
        name="FastJudge_v2", teacher="judge",
        student_base="phi-3.5-mini", backend="stub",
        artifact_path=str(tmp_path / "b.bin"), rows_used=15,
        status="staging",
    )
    store.write(a)
    store.write(b)
    store.promote(b)
    statuses = {s.name: s.status for s in store.list()}
    assert statuses["FastJudge_v2"] == "production"
    assert statuses["FastJudge_v1"] == "archived"
