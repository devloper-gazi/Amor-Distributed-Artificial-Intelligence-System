"""
Unit tests for ``document_processor/quick_code/contracts.py``.

These cover the V2 typed IR — enum coercion, frozen invariants,
``extra="forbid"`` rejection, sub-task graph validation, and the
``TaskIR.from_quick_code_request`` adapter.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from document_processor.quick_code.contracts import (
    ContractCondition,
    CodeSnippet,
    MCTSNode,
    PreferencePair,
    SandboxResult,
    SandboxTier,
    SSEEvent,
    SubTask,
    SymValidationResult,
    TaskComplexity,
    TaskIR,
    TestResult,
)
from document_processor.quick_code.models import QuickCodeRequest


# ─────────────────────────────────────────────────────────────────────
# Enum coercion
# ─────────────────────────────────────────────────────────────────────


def test_task_complexity_coerce_known_values():
    assert TaskComplexity.coerce("trivial") is TaskComplexity.TRIVIAL
    assert TaskComplexity.coerce(" simple ") is TaskComplexity.SIMPLE
    assert TaskComplexity.coerce("COMPLEX") is TaskComplexity.COMPLEX
    assert TaskComplexity.coerce("Math") is TaskComplexity.MATH


def test_task_complexity_coerce_unknown_returns_none():
    assert TaskComplexity.coerce("unknown") is None
    assert TaskComplexity.coerce("") is None
    assert TaskComplexity.coerce(None) is None
    assert TaskComplexity.coerce(123) is None


def test_sandbox_tier_coerce_defaults_to_quick():
    assert SandboxTier.coerce("quick") is SandboxTier.QUICK
    assert SandboxTier.coerce("PRO") is SandboxTier.PRO
    assert SandboxTier.coerce("nonsense") is SandboxTier.QUICK
    assert SandboxTier.coerce(None) is SandboxTier.QUICK


# ─────────────────────────────────────────────────────────────────────
# ContractCondition + frozen / extra="forbid"
# ─────────────────────────────────────────────────────────────────────


def test_contract_condition_basic():
    c = ContractCondition(kind="pre", expression="x > 0", description="non-empty")
    assert c.kind == "pre"
    assert c.expression == "x > 0"


def test_contract_condition_rejects_unknown_kind():
    with pytest.raises(ValidationError):
        ContractCondition(kind="invariant", expression="x > 0")  # type: ignore[arg-type]


def test_contract_condition_rejects_empty_expression():
    with pytest.raises(ValidationError):
        ContractCondition(kind="pre", expression="")


def test_contract_condition_extra_forbid():
    with pytest.raises(ValidationError):
        ContractCondition(  # type: ignore[call-arg]
            kind="pre", expression="x>0", priority=5
        )


def test_contract_condition_is_frozen():
    c = ContractCondition(kind="pre", expression="x > 0")
    with pytest.raises(ValidationError):
        c.expression = "y > 0"  # type: ignore[misc]


# ─────────────────────────────────────────────────────────────────────
# SubTask + TaskIR graph validation
# ─────────────────────────────────────────────────────────────────────


def test_subtask_basic():
    st = SubTask(id="a1", title="Step one", description="do something")
    assert st.id == "a1"
    assert st.dependencies == []


def test_taskir_accepts_well_formed_graph():
    ir = TaskIR(
        id="t1",
        prompt="reverse a string",
        subtasks=[
            SubTask(id="a", title="prep"),
            SubTask(id="b", title="reverse", dependencies=["a"]),
        ],
    )
    assert len(ir.subtasks) == 2
    assert ir.subtasks[1].dependencies == ["a"]


def test_taskir_rejects_duplicate_subtask_ids():
    with pytest.raises(ValidationError, match="duplicate"):
        TaskIR(
            id="t1",
            prompt="reverse a string",
            subtasks=[
                SubTask(id="a", title="prep"),
                SubTask(id="a", title="prep again"),
            ],
        )


def test_taskir_rejects_unknown_dependency():
    with pytest.raises(ValidationError, match="unknown id"):
        TaskIR(
            id="t1",
            prompt="reverse a string",
            subtasks=[SubTask(id="a", title="prep", dependencies=["does_not_exist"])],
        )


def test_taskir_rejects_self_dependency():
    with pytest.raises(ValidationError, match="depends on itself"):
        TaskIR(
            id="t1",
            prompt="reverse a string",
            subtasks=[SubTask(id="a", title="loop", dependencies=["a"])],
        )


def test_taskir_extra_forbid():
    with pytest.raises(ValidationError):
        TaskIR(  # type: ignore[call-arg]
            id="t1", prompt="reverse a string", surprise=True
        )


# ─────────────────────────────────────────────────────────────────────
# TaskIR.from_quick_code_request adapter
# ─────────────────────────────────────────────────────────────────────


def test_from_quick_code_request_minimal():
    req = QuickCodeRequest(prompt="reverse a string")
    ir = TaskIR.from_quick_code_request(req, ir_id="t1")
    assert ir.prompt == "reverse a string"
    assert ir.mode is SandboxTier.QUICK
    assert ir.complexity is None
    assert ir.metadata["effort"] == "medium"


def test_from_quick_code_request_pro_mode():
    req = QuickCodeRequest(prompt="design lru cache")
    # The legacy dataclass does not yet have ``mode`` / ``complexity_hint``
    # fields — we bolt them on with ``setattr`` so this test stays
    # valid even before the models.py extension lands in the next
    # implementation step.
    setattr(req, "mode", "pro")
    setattr(req, "complexity_hint", "complex")
    ir = TaskIR.from_quick_code_request(req, ir_id="t2")
    assert ir.mode is SandboxTier.PRO
    assert ir.complexity is TaskComplexity.COMPLEX


def test_from_quick_code_request_invalid_complexity_hint():
    req = QuickCodeRequest(prompt="x")
    setattr(req, "complexity_hint", "weird")
    ir = TaskIR.from_quick_code_request(req, ir_id="t3")
    assert ir.complexity is None  # silently dropped, not an error


# ─────────────────────────────────────────────────────────────────────
# Execution / verification records
# ─────────────────────────────────────────────────────────────────────


def test_test_result_frozen():
    r = TestResult(name="t_basic", passed=True, duration_ms=12.5)
    with pytest.raises(ValidationError):
        r.passed = False  # type: ignore[misc]


def test_test_result_negative_duration_rejected():
    with pytest.raises(ValidationError):
        TestResult(name="t", passed=True, duration_ms=-1.0)


def test_sandbox_result_default_safe():
    r = SandboxResult(ok=False)
    assert r.stdout == ""
    assert r.exit_code == 0
    assert r.tier is SandboxTier.QUICK


def test_sandbox_result_from_dict_legacy_shape():
    r = SandboxResult.from_dict(
        {
            "passed": True,
            "stdout": "OK",
            "exit_code": 0,
            "duration_ms": 42.0,
            "tier": "pro",
        }
    )
    assert r.ok is True
    assert r.tier is SandboxTier.PRO


def test_sandbox_result_from_dict_none_safe():
    r = SandboxResult.from_dict(None)
    assert r.ok is False


def test_sandbox_result_from_dict_clamps_long_streams():
    long_text = "x" * 20000
    r = SandboxResult.from_dict({"ok": True, "stdout": long_text})
    assert len(r.stdout) == 8000


def test_symvalidation_iterations_bounded():
    SymValidationResult(ok=True, iterations=3)  # OK at max
    with pytest.raises(ValidationError):
        SymValidationResult(ok=True, iterations=4)


# ─────────────────────────────────────────────────────────────────────
# Retrieval + tournament
# ─────────────────────────────────────────────────────────────────────


def test_code_snippet_basic():
    s = CodeSnippet(source="def f(): pass", score=0.42, source_path="x.py")
    assert s.score == 0.42
    assert s.language == "python"


def test_mcts_node_mutable_visit_count():
    n = MCTSNode(id="n1", code="pass")
    n.visit_count += 1
    n.score += 0.5
    assert n.visit_count == 1
    assert n.score == 0.5


# ─────────────────────────────────────────────────────────────────────
# Preference + telemetry
# ─────────────────────────────────────────────────────────────────────


def test_preference_pair_required_fields():
    p = PreferencePair(prompt="task", chosen="winner", rejected="loser")
    assert p.reward_delta == 0.0


def test_preference_pair_rejects_empty_chosen():
    with pytest.raises(ValidationError):
        PreferencePair(prompt="task", chosen="", rejected="loser")


def test_sse_event_default_timestamp():
    e = SSEEvent(type="phase_start")
    assert e.timestamp > 0
    assert e.payload == {}


def test_sse_event_extra_forbid():
    with pytest.raises(ValidationError):
        SSEEvent(type="phase_start", surprise=1)  # type: ignore[call-arg]
