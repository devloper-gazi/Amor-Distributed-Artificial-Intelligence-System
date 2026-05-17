"""Cycle H.2 — LazyGraphRAG benchmark tool coverage.

Tests the pure-Python helpers + CLI scaffolding without booting the
full LanceDB / sentence-transformers stack.  End-to-end nDCG numbers
need real corpus + index — that's the operator-run benchmark.
"""

from __future__ import annotations

import json
import math
from pathlib import Path

import pytest


# ─── ndcg_at_k ───────────────────────────────────────────────────────


def test_ndcg_perfect_ranking():
    """All retrieved docs are relevant + ordered → nDCG@k = 1.0."""
    from tools.eval.lazy_graphrag_bench import ndcg_at_k
    assert ndcg_at_k(["a", "b", "c"], ["a", "b", "c"], k=10) == 1.0


def test_ndcg_all_top_k_relevant():
    """Smaller relevant set fully covered by retrieved top-K → 1.0.
    Documenting the ideal-DCG semantics: even when retrieved has
    extra noise BELOW the relevant set, the score stays 1.0."""
    from tools.eval.lazy_graphrag_bench import ndcg_at_k
    assert ndcg_at_k(["a", "b", "noise"], ["a", "b"], k=10) == 1.0


def test_ndcg_zero_when_no_overlap():
    """Retrieved has none of the relevant docs → nDCG = 0.0."""
    from tools.eval.lazy_graphrag_bench import ndcg_at_k
    assert ndcg_at_k(["x", "y", "z"], ["a", "b"], k=10) == 0.0


def test_ndcg_zero_when_no_relevant():
    """Empty relevant list → return 0.0 (avoid div-by-zero)."""
    from tools.eval.lazy_graphrag_bench import ndcg_at_k
    assert ndcg_at_k(["a", "b", "c"], [], k=10) == 0.0


def test_ndcg_intermediate_score():
    """One relevant doc at position 2 (of 5), one at position 4.
    Expected nDCG@10 ≈ 0.65 (verified analytically)."""
    from tools.eval.lazy_graphrag_bench import ndcg_at_k
    score = ndcg_at_k(["x", "a", "y", "b", "z"], ["a", "b"], k=10)
    assert abs(score - 0.6509) < 0.001


def test_ndcg_truncation_to_k():
    """Same retrieval but k=2 → only top-2 (`x`, `a`) considered;
    only `a` is relevant at rank 2 → nDCG@2 ≈ 0.387."""
    from tools.eval.lazy_graphrag_bench import ndcg_at_k
    score = ndcg_at_k(["x", "a", "y", "b", "z"], ["a", "b"], k=2)
    assert abs(score - 0.387) < 0.01


# ─── BenchResult serialization ──────────────────────────────────────


def test_bench_result_to_dict_has_v20_gate_field():
    """The persisted snapshot must surface ``ndcg_uplift_pct`` so v20
    gate condition #5 resolves it without column-walking."""
    from tools.eval.lazy_graphrag_bench import BenchResult
    r = BenchResult(
        threshold_uplift_pct=15.0,
        ndcg_baseline_mean=0.40,
        ndcg_graphrag_mean=0.50,
        ndcg_uplift_pct=25.0,
    )
    payload = r.to_dict()
    assert payload["ndcg_uplift_pct"] == 25.0
    assert payload["threshold_uplift_pct"] == 15.0
    assert payload["ndcg_baseline_mean"] == 0.40
    assert payload["ndcg_graphrag_mean"] == 0.50
    assert "per_query" in payload
    assert "notes" in payload


# ─── Query bench file shape ─────────────────────────────────────────


def test_seed_bench_file_loadable_and_well_formed():
    """The H.2 seed file must validate against the basic shape the
    bench tool expects.  Each query has `id`, `query`, and
    `relevant_source_ids`."""
    seed_path = (
        Path(__file__).resolve().parent.parent.parent
        / "tests" / "eval" / "lazy_graphrag_100_questions.json"
    )
    assert seed_path.is_file(), f"missing seed: {seed_path}"
    payload = json.loads(seed_path.read_text(encoding="utf-8"))
    queries = payload.get("queries") or []
    # Seed currently has 20 queries; expand to 100 over Cycle I+ work.
    assert len(queries) >= 20
    for q in queries:
        assert q.get("id"), f"missing id in query: {q}"
        assert q.get("query"), f"missing query text: {q['id']}"
        assert isinstance(q.get("relevant_source_ids"), list), q["id"]
        assert q["relevant_source_ids"], f"empty relevant set for {q['id']}"
        # Source IDs are repo-relative paths; validate they look reasonable.
        for sid in q["relevant_source_ids"]:
            assert isinstance(sid, str)
            assert sid, "blank source id"
            assert "\0" not in sid


def test_seed_bench_query_ids_are_unique():
    """Duplicate ids break the bench result aggregation."""
    seed_path = (
        Path(__file__).resolve().parent.parent.parent
        / "tests" / "eval" / "lazy_graphrag_100_questions.json"
    )
    payload = json.loads(seed_path.read_text(encoding="utf-8"))
    ids = [q["id"] for q in payload["queries"]]
    assert len(ids) == len(set(ids)), "duplicate query ids in seed"


# ─── CLI argparse ───────────────────────────────────────────────────


def test_cli_defaults_match_v20_gate_thresholds():
    """Plan-agent locked: --top-k=10, --threshold-pct=15.0 mirror v20
    gate condition #5 (`lazygraphrag_ndcg_uplift_pct >= 15`)."""
    from tools.eval.lazy_graphrag_bench import build_parser
    args = build_parser().parse_args([])
    assert args.top_k == 10
    assert args.threshold_pct == 15.0
    assert args.json is False
