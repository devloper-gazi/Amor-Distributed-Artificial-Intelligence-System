"""
Tests for the MeshAggregator — alternative merging, dedup, weighted
composite scoring, and the per-specialist visibility envelope.
"""

from __future__ import annotations

from document_processor.code_intelligence.mesh.aggregator import (
    AggregatedReasoning, MeshAggregator,
)
from document_processor.code_intelligence.mesh.specialists import (
    SpecialistOutput,
)


def _alt(label, summary, scores, ce="O(n)"):
    return {"label": label, "summary": summary, "scores": scores,
            "complexity_estimate": ce}


def _out(role, alts, chosen, error=None):
    role_label_map = {
        "general": "General Reasoner", "math": "Mathematics Specialist",
        "performance": "Performance Analyst", "edge_case": "Edge-Case Hunter",
    }
    return SpecialistOutput(
        role=role, role_label=role_label_map[role],
        parsed={"alternatives": alts, "chosen": chosen, "rationale": "ok"}
        if not error else {},
        error=error,
    )


# ── happy path ────────────────────────────────────────────────────


def test_merge_consensus_of_two_specialists_collapses_to_one_alt():
    """When two specialists propose the SAME approach (identical
    summary), the aggregator must collapse them into a single
    alternative whose scores are the weighted average across both."""
    summary = "iterative loop with O(n) walk"
    a_general = _alt("A", summary,
                     {"clarity": 0.8, "math_soundness": 0.7,
                      "performance": 0.7, "edge_cases": 0.7})
    a_math = _alt("A", summary,
                  {"clarity": 0.6, "math_soundness": 0.95,
                   "performance": 0.6, "edge_cases": 0.6})
    # Performance specialist proposes a *distinct* approach with a
    # different summary — this one should NOT collapse with the others.
    a_perf = _alt("X", "vectorised numpy sum",
                  {"clarity": 0.7, "math_soundness": 0.6,
                   "performance": 0.95, "edge_cases": 0.7})

    outs = [
        _out("general", [a_general], "A"),
        _out("math", [a_math], "A"),
        _out("performance", [a_perf], "X"),
    ]
    agg = MeshAggregator().merge(outs)
    assert isinstance(agg, AggregatedReasoning)
    # general+math collapse → 1 merged; perf stays distinct → 2 total.
    assert len(agg.reasoning.alternatives) == 2
    # Find the merged alt (the one general+math agreed on).
    merged = next(
        a for a in agg.reasoning.alternatives
        if a.summary == summary
    )
    # Math specialist's high math_soundness (0.95) should dominate
    # via the 1.5× specialty weight on math_soundness.
    #   weighted = (1.0 * 0.7 + 1.5 * 0.95) / (1.0 + 1.5) = 2.125 / 2.5 = 0.85
    assert merged.scores["math_soundness"] > 0.80
    # consensus_count counts alternatives with ≥2 specialist votes.
    assert agg.consensus_count == 1


def test_merge_distinct_summaries_kept_separate():
    a_general = _alt("A", "iterative loop",
                     {"clarity": 0.8, "math_soundness": 0.7,
                      "performance": 0.7, "edge_cases": 0.7})
    a_math = _alt("B", "recursive descent",
                  {"clarity": 0.6, "math_soundness": 0.9,
                   "performance": 0.5, "edge_cases": 0.6})
    outs = [
        _out("general", [a_general], "A"),
        _out("math", [a_math], "B"),
    ]
    agg = MeshAggregator().merge(outs)
    assert len(agg.reasoning.alternatives) == 2
    labels = {a.label for a in agg.reasoning.alternatives}
    assert labels == {"A", "B"}


def test_merge_picks_highest_composite():
    # B has the higher composite — aggregator must pick B regardless
    # of any specialist's `chosen` field.
    a = _alt("A", "low everything",
             {"clarity": 0.3, "math_soundness": 0.3,
              "performance": 0.3, "edge_cases": 0.3})
    b = _alt("B", "high everything",
             {"clarity": 0.9, "math_soundness": 0.9,
              "performance": 0.9, "edge_cases": 0.9})
    outs = [
        _out("general", [a, b], "A"),  # specialist picked A (worse)
        _out("math",    [a, b], "A"),
    ]
    agg = MeshAggregator().merge(outs)
    # Aggregator labels are re-emitted A/B/C/D in composite-descending
    # order, so the top-scoring alt is always 'A' after merge.
    assert agg.reasoning.chosen_label == "A"
    assert agg.reasoning.alternatives[0].composite >= agg.reasoning.alternatives[-1].composite


def test_per_specialist_picks_recorded():
    a = _alt("A", "iterative",
             {"clarity": 0.8, "math_soundness": 0.8,
              "performance": 0.7, "edge_cases": 0.7})
    b = _alt("B", "recursive",
             {"clarity": 0.6, "math_soundness": 0.9,
              "performance": 0.5, "edge_cases": 0.6})
    outs = [
        _out("general", [a], "A"),
        _out("math",    [a, b], "B"),
        _out("performance", [a], "A"),
        _out("edge_case",   [a], "A"),
    ]
    agg = MeshAggregator().merge(outs)
    assert agg.per_specialist_picks == {
        "general": "A", "math": "B",
        "performance": "A", "edge_case": "A",
    }


# ── error paths ────────────────────────────────────────────────────


def test_all_specialists_failed_synthesises_fallback():
    outs = [
        _out("general", [], "", error="LLM unreachable"),
        _out("math",    [], "", error="JSON parse failed"),
    ]
    agg = MeshAggregator().merge(outs)
    assert len(agg.reasoning.alternatives) == 1
    # Synthesised fallback has all 0.5 scores.
    assert agg.reasoning.alternatives[0].composite == 0.5
    assert agg.specialist_errors == {
        "general": "LLM unreachable",
        "math": "JSON parse failed",
    }
    assert any("all specialists failed" in f for f in agg.findings)


def test_partial_failure_still_produces_aggregate():
    a = _alt("A", "ok approach",
             {"clarity": 0.8, "math_soundness": 0.8,
              "performance": 0.8, "edge_cases": 0.8})
    outs = [
        _out("general", [a], "A"),
        _out("math",    [], "", error="boom"),
    ]
    agg = MeshAggregator().merge(outs)
    # General contributed an alternative; math failed but didn't block.
    assert len(agg.reasoning.alternatives) == 1
    assert agg.specialist_errors == {"math": "boom"}


def test_alt_count_recorded_per_specialist():
    a = _alt("A", "x", {"clarity": 0.5, "math_soundness": 0.5,
                         "performance": 0.5, "edge_cases": 0.5})
    b = _alt("B", "y", {"clarity": 0.5, "math_soundness": 0.5,
                         "performance": 0.5, "edge_cases": 0.5})
    outs = [
        _out("general", [a, b], "A"),
        _out("math", [a], "A"),
    ]
    agg = MeshAggregator().merge(outs)
    assert agg.specialist_alt_counts == {"general": 2, "math": 1}


def test_specialty_weight_amplifies_owner_axis():
    """Math reasoner's math_soundness=0.9 should outweigh general's 0.5
    even if both contribute to the same merged alt."""
    a_general = _alt("A", "shared approach",
                     {"clarity": 0.5, "math_soundness": 0.5,
                      "performance": 0.5, "edge_cases": 0.5})
    a_math = _alt("A", "shared approach",
                  {"clarity": 0.5, "math_soundness": 0.9,
                   "performance": 0.5, "edge_cases": 0.5})
    outs = [
        _out("general", [a_general], "A"),
        _out("math", [a_math], "A"),
    ]
    agg = MeshAggregator().merge(outs)
    merged = agg.reasoning.alternatives[0]
    # Math's 0.9 with 1.5× weight + general's 0.5 with 1.0×:
    #   numerator = 1.5 * 0.9 + 1.0 * 0.5 = 1.85
    #   denominator = 1.5 + 1.0 = 2.5
    #   blended math_soundness = 0.74
    assert 0.70 < merged.scores["math_soundness"] < 0.78
    # Other axes (no specialty bias) are simple average = 0.5
    assert merged.scores["clarity"] == 0.5
