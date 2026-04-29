"""
Tests for the QuickCode dataclasses — composite scoring formula,
normalize() clamping, and the to_implementation_artifact() adapter
shape (so Consortium can absorb a QuickCodeBundle without bespoke
conversion code).
"""

from __future__ import annotations

import pytest

from document_processor.quick_code.models import (
    COMPOSITE_WEIGHTS,
    MAX_REFINE_ITERATIONS,
    QuickCodeAlternative,
    QuickCodeBundle,
    QuickCodeGate,
    QuickCodeReasoning,
    QuickCodeRequest,
    QuickCodeVerification,
)


# ── composite_score formula ─────────────────────────────────────────


def test_composite_weights_sum_to_one():
    """The four weights must sum to exactly 1.0 — otherwise scores
    drift outside [0,1] silently."""
    assert sum(COMPOSITE_WEIGHTS.values()) == pytest.approx(1.0)


def test_composite_score_full_payload():
    score = QuickCodeAlternative.composite_score({
        "clarity": 0.8, "math_soundness": 0.9,
        "performance": 0.6, "edge_cases": 0.7,
    })
    # 0.30*0.8 + 0.30*0.9 + 0.20*0.6 + 0.20*0.7 = 0.24+0.27+0.12+0.14 = 0.77
    assert score == pytest.approx(0.77)


def test_composite_score_missing_axes_treated_as_zero():
    score = QuickCodeAlternative.composite_score({"clarity": 1.0})
    # Only clarity is set → 0.30 * 1.0 = 0.30
    assert score == pytest.approx(0.30)


def test_composite_score_clamps_out_of_range():
    score = QuickCodeAlternative.composite_score({
        "clarity": 5.0,   # clamps to 1.0
        "math_soundness": -1.0,  # clamps to 0.0
        "performance": 0.5, "edge_cases": 0.5,
    })
    # 0.30*1.0 + 0.30*0.0 + 0.20*0.5 + 0.20*0.5 = 0.30+0+0.10+0.10 = 0.50
    assert score == pytest.approx(0.50)


def test_composite_score_empty_returns_zero():
    assert QuickCodeAlternative.composite_score({}) == 0.0
    assert QuickCodeAlternative.composite_score(None) == 0.0


def test_composite_score_handles_garbage():
    score = QuickCodeAlternative.composite_score({
        "clarity": "nan-string", "math_soundness": None,
        "performance": [1, 2], "edge_cases": 0.4,
    })
    # garbage → 0.0; only edge_cases contributes 0.20*0.4 = 0.08
    assert score == pytest.approx(0.08)


def test_alternative_property_matches_classmethod():
    a = QuickCodeAlternative(
        label="X",
        scores={"clarity": 0.5, "math_soundness": 0.5,
                "performance": 0.5, "edge_cases": 0.5},
    )
    assert a.composite == QuickCodeAlternative.composite_score(a.scores)


# ── QuickCodeRequest.normalize() ────────────────────────────────────


def test_normalize_clamps_max_refine_to_cap():
    r = QuickCodeRequest(prompt="x", max_refine=99)
    r.normalize()
    assert r.max_refine == MAX_REFINE_ITERATIONS == 3


def test_normalize_clamps_negative_to_zero():
    r = QuickCodeRequest(prompt="x", max_refine=-5)
    r.normalize()
    assert r.max_refine == 0


def test_normalize_zeroes_when_allow_refine_false():
    r = QuickCodeRequest(prompt="x", max_refine=2, allow_refine=False)
    r.normalize()
    assert r.max_refine == 0


def test_normalize_idempotent():
    r = QuickCodeRequest(prompt="x", max_refine=2)
    r.normalize().normalize()
    assert r.max_refine == 2


# ── Reasoning.chosen property ───────────────────────────────────────


def test_reasoning_chosen_picks_by_label():
    a1 = QuickCodeAlternative(label="A", scores={"clarity": 0.1})
    a2 = QuickCodeAlternative(label="B", scores={"clarity": 0.9})
    r = QuickCodeReasoning(alternatives=[a1, a2], chosen_label="B")
    assert r.chosen is a2


def test_reasoning_chosen_falls_back_to_first():
    a1 = QuickCodeAlternative(label="A")
    a2 = QuickCodeAlternative(label="B")
    r = QuickCodeReasoning(alternatives=[a1, a2], chosen_label="X")
    # Label not found → first alternative.
    assert r.chosen is a1


def test_reasoning_chosen_none_when_empty():
    r = QuickCodeReasoning(alternatives=[], chosen_label="A")
    assert r.chosen is None


# ── to_implementation_artifact adapter shape ────────────────────────


def test_to_implementation_artifact_has_consortium_keys():
    """Every key the consortium ImplementationArtifact dataclass exposes
    must be present in our bundle's adapter output. Pinning this shape
    is what lets Consortium swap engines without bespoke conversion."""
    from document_processor.consortium.models import ImplementationArtifact

    bundle = QuickCodeBundle(
        session_id="s1",
        request=QuickCodeRequest(prompt="x", language="python"),
        triage={"language": "python", "task_type": "generation"},
        reasoning=QuickCodeReasoning(
            alternatives=[QuickCodeAlternative(
                label="A", summary="approach A",
                scores={"clarity": 0.8, "math_soundness": 0.9,
                        "performance": 0.7, "edge_cases": 0.8},
                complexity_estimate="O(n)",
            )],
            chosen_label="A",
            rationale="A is the simplest one",
        ),
        code="print('hi')",
        tests="def test(): assert 1",
        verification=QuickCodeVerification(
            execution={"success": True, "exit_code": 0},
            static={"severity_counts": {"error": 0}},
            score=85.0,
        ),
        refine_iterations=1,
        deliverable_markdown="# Done",
        models_used={"reasoner": "qwen2.5:7b"},
    )

    art = bundle.to_implementation_artifact()
    assert isinstance(art, ImplementationArtifact)

    # The adapter must populate every consortium-side field at least to
    # a sensible default; downstream gate scoring relies on each.
    art_d = art.to_dict()
    expected_keys = {
        "code", "tests", "language", "plan", "triage",
        "static_analysis", "execution_results", "review",
        "deliverable_markdown", "models_used", "debug_iterations",
    }
    assert expected_keys.issubset(art_d.keys())

    # plan tags the engine so consortium's bundle JSON shows which
    # implementation engine was used.
    assert art_d["plan"]["engine"] == "quick_code"
    assert art_d["plan"]["chosen_label"] == "A"
    assert art_d["plan"]["alternatives_considered"] == 1

    # Review payload is real (not empty) — consortium's
    # _gate_implementation reads from it.
    assert art_d["review"]["verdict"] in {"approve", "approve_with_changes"}
    assert isinstance(art_d["review"]["strengths"], list)


def test_to_implementation_artifact_handles_missing_reasoning():
    """Cancelled / errored runs don't have reasoning — adapter must
    still produce a valid artifact rather than raise."""
    bundle = QuickCodeBundle(
        session_id="s2",
        request=QuickCodeRequest(prompt="x"),
    )
    art = bundle.to_implementation_artifact()
    # Empty review is fine — consortium's gate will treat absent review
    # as a finding, not a crash.
    assert art.code is None
    assert art.review == {}
    assert art.plan["engine"] == "quick_code"


def test_to_implementation_artifact_failed_verification_marks_changes():
    bundle = QuickCodeBundle(
        session_id="s3",
        request=QuickCodeRequest(prompt="x"),
        reasoning=QuickCodeReasoning(
            alternatives=[QuickCodeAlternative(
                label="A",
                scores={"clarity": 0.5, "math_soundness": 0.5,
                        "performance": 0.3, "edge_cases": 0.4},
            )],
            chosen_label="A",
        ),
        verification=QuickCodeVerification(score=55.0),
    )
    art = bundle.to_implementation_artifact()
    # Verification < 70 → adapter emits "approve_with_changes".
    assert art.review["verdict"] == "approve_with_changes"
    # Low performance score → weakness with a perf line.
    assert any(
        w.get("title") == "performance unproven"
        for w in art.review.get("weaknesses", [])
    )


# ── Gate dataclass shape ────────────────────────────────────────────


def test_gate_to_dict_round_trip():
    g = QuickCodeGate(
        phase="reason", status="passed", score=85.0,
        findings=["all four axes scored"], summary="ok",
    )
    d = g.to_dict()
    assert d["phase"] == "reason"
    assert d["status"] == "passed"
    assert d["score"] == 85.0
    assert d["findings"] == ["all four axes scored"]


# ── Bundle.to_dict serialisation ────────────────────────────────────


def test_bundle_to_dict_omits_raw_llm():
    """raw_llm can be hundreds of KB — it's debugging-only and must
    NOT leak into the public bundle JSON."""
    bundle = QuickCodeBundle(
        session_id="s4",
        request=QuickCodeRequest(prompt="x"),
        reasoning=QuickCodeReasoning(
            alternatives=[QuickCodeAlternative(label="A")],
            raw_llm="X" * 50_000,  # huge debug payload
        ),
    )
    d = bundle.to_dict()
    # The reasoning dict must not contain raw_llm.
    assert "raw_llm" not in d["reasoning"]
