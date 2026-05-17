#!/usr/bin/env python3
"""Cycle H.2 — LazyGraphRAG vs LanceDB-only retrieval benchmark.

Runs the multi-hop query bench from
``tests/eval/lazy_graphrag_100_questions.json`` against BOTH retrieval
paths and computes the nDCG@10 uplift.  v20 gate condition #5 reads
``ndcg_uplift_pct`` from the JSON snapshot this script writes:

    data/baselines/lazygraphrag_bench_latest.json

Pre-requisites:
  * AMOR's LanceDB corpus is populated with the documents the queries
    reference (the bench's `relevant_source_ids` field is the gold
    ranking — chunks NOT in the corpus are silently ignored).
  * ``settings.rag_graphrag_enabled=True`` for the LazyGraphRAG run.
  * The entity-graph index is built (call
    ``LanceDBVectorStore.build_lazy_graphrag_index()`` once before
    the bench OR rely on the lazy build inside the search path).

Usage::

    python tools/eval/lazy_graphrag_bench.py
    python tools/eval/lazy_graphrag_bench.py \\
        --queries tests/eval/lazy_graphrag_100_questions.json \\
        --top-k 10 \\
        --json

Exit codes:
  0   uplift ≥ threshold (v20 gate condition #5 lifted)
  1   uplift < threshold OR bench failed
  2   corpus/import error
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import math
import os
import statistics
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

if sys.platform == "win32":
    for _s in (sys.stdout, sys.stderr):
        _r = getattr(_s, "reconfigure", None)
        if _r is not None:
            try:
                _r(encoding="utf-8", errors="replace")
            except (OSError, ValueError):
                pass

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
logger = logging.getLogger(__name__)


REPO_ROOT = Path(__file__).resolve().parent.parent.parent
DEFAULT_QUERIES = REPO_ROOT / "tests" / "eval" / "lazy_graphrag_100_questions.json"
OUT_ROOT = REPO_ROOT / "data" / "baselines"


# ─── Metrics ────────────────────────────────────────────────────────


def ndcg_at_k(
    retrieved: List[str],
    relevant: List[str],
    *,
    k: int = 10,
) -> float:
    """Discounted Cumulative Gain at k, normalised by the ideal DCG.

    Uses binary relevance (the doc is in `relevant` or it isn't),
    which matches the bench's gold-ranking shape.  Returns 0.0 when
    there are no relevant docs (avoid div-by-zero rather than NaN)."""
    if not relevant:
        return 0.0
    retrieved_top = retrieved[:k]
    relevant_set = set(relevant)
    dcg = 0.0
    for i, doc_id in enumerate(retrieved_top, start=1):
        rel = 1.0 if doc_id in relevant_set else 0.0
        if rel > 0:
            dcg += rel / math.log2(i + 1)
    # Ideal DCG — first min(k, |relevant|) positions hold rel=1.
    ideal_count = min(k, len(relevant))
    idcg = sum(
        1.0 / math.log2(i + 1) for i in range(1, ideal_count + 1)
    )
    return dcg / idcg if idcg > 0 else 0.0


# ─── Runner ─────────────────────────────────────────────────────────


@dataclass
class PerQueryResult:
    query_id: str
    query: str
    relevant: List[str]
    retrieved_baseline: List[str] = field(default_factory=list)
    retrieved_graphrag: List[str] = field(default_factory=list)
    ndcg_baseline: float = 0.0
    ndcg_graphrag: float = 0.0
    elapsed_baseline_ms: float = 0.0
    elapsed_graphrag_ms: float = 0.0


@dataclass
class BenchResult:
    threshold_uplift_pct: float
    ndcg_baseline_mean: float
    ndcg_graphrag_mean: float
    ndcg_uplift_pct: float
    per_query: List[PerQueryResult] = field(default_factory=list)
    started_utc: str = ""
    finished_utc: str = ""
    notes: List[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "threshold_uplift_pct": self.threshold_uplift_pct,
            "ndcg_baseline_mean": round(self.ndcg_baseline_mean, 4),
            "ndcg_graphrag_mean": round(self.ndcg_graphrag_mean, 4),
            "ndcg_uplift_pct": round(self.ndcg_uplift_pct, 2),
            "started_utc": self.started_utc,
            "finished_utc": self.finished_utc,
            "per_query": [
                {
                    "query_id": q.query_id,
                    "query": q.query,
                    "relevant": q.relevant,
                    "ndcg_baseline": round(q.ndcg_baseline, 4),
                    "ndcg_graphrag": round(q.ndcg_graphrag, 4),
                    "elapsed_baseline_ms": round(q.elapsed_baseline_ms, 1),
                    "elapsed_graphrag_ms": round(q.elapsed_graphrag_ms, 1),
                }
                for q in self.per_query
            ],
            "notes": self.notes,
        }


def _load_queries(path: Path) -> List[Dict[str, Any]]:
    if not path.is_file():
        raise FileNotFoundError(f"query bench file missing: {path}")
    payload = json.loads(path.read_text(encoding="utf-8"))
    return payload.get("queries") or []


async def _retrieve_baseline(store, query: str, *, top_k: int) -> Tuple[List[str], float]:
    t0 = time.time()
    hits = await store.search(query, limit=top_k)
    elapsed = (time.time() - t0) * 1000.0
    ids = [
        (h.get("document_id") or h.get("source_url") or h.get("id") or "")
        for h in hits
    ]
    return ids, elapsed


async def _run_bench(args: argparse.Namespace) -> BenchResult:
    try:
        from local_ai.vector_store.lancedb_store import LanceDBVectorStore
        from document_processor.config.settings import settings
    except Exception as exc:
        logger.error("import failed: %s", exc)
        raise

    queries = _load_queries(Path(args.queries))
    if not queries:
        raise RuntimeError(f"no queries loaded from {args.queries}")

    result = BenchResult(
        threshold_uplift_pct=args.threshold_pct,
        ndcg_baseline_mean=0.0,
        ndcg_graphrag_mean=0.0,
        ndcg_uplift_pct=0.0,
        started_utc=datetime.now(timezone.utc).isoformat(),
    )

    # Build / open the LanceDB store.  The bench uses the same singleton
    # the production engine instantiates; we toggle
    # `settings.rag_graphrag_enabled` between runs.
    store = LanceDBVectorStore()

    # Phase 1: LanceDB-only.
    original_flag = settings.rag_graphrag_enabled
    try:
        settings.rag_graphrag_enabled = False
        store._lazy_graphrag_config = None       # force re-resolve
        for q in queries:
            r = PerQueryResult(
                query_id=q["id"],
                query=q["query"],
                relevant=q.get("relevant_source_ids") or [],
            )
            ids, elapsed = await _retrieve_baseline(store, r.query, top_k=args.top_k)
            r.retrieved_baseline = ids
            r.ndcg_baseline = ndcg_at_k(ids, r.relevant, k=args.top_k)
            r.elapsed_baseline_ms = elapsed
            result.per_query.append(r)

        # Phase 2: LazyGraphRAG-on.  Lazy-build the index if missing.
        if store._lazy_graphrag_index is None:
            logger.info("building LazyGraphRAG index (one-shot, may take 5-30 min)")
            try:
                stats = await store.build_lazy_graphrag_index()
                result.notes.append(
                    f"index built: chunks={stats['chunk_count']}, "
                    f"entities={stats['entity_count']}, "
                    f"communities={stats['community_count']}, "
                    f"build={stats['build_duration_s']:.1f}s",
                )
            except Exception as exc:
                logger.error("index build failed; skipping LazyGraphRAG phase: %s", exc)
                result.notes.append(f"index_build_failed: {exc}")
        settings.rag_graphrag_enabled = True
        store._lazy_graphrag_config = None       # force re-resolve
        for r in result.per_query:
            ids, elapsed = await _retrieve_baseline(store, r.query, top_k=args.top_k)
            r.retrieved_graphrag = ids
            r.ndcg_graphrag = ndcg_at_k(ids, r.relevant, k=args.top_k)
            r.elapsed_graphrag_ms = elapsed
    finally:
        settings.rag_graphrag_enabled = original_flag

    # Aggregate.
    bm = statistics.mean(q.ndcg_baseline for q in result.per_query) if result.per_query else 0.0
    gm = statistics.mean(q.ndcg_graphrag for q in result.per_query) if result.per_query else 0.0
    result.ndcg_baseline_mean = bm
    result.ndcg_graphrag_mean = gm
    if bm > 0:
        result.ndcg_uplift_pct = ((gm - bm) / bm) * 100.0
    else:
        result.ndcg_uplift_pct = 0.0
    result.finished_utc = datetime.now(timezone.utc).isoformat()
    return result


def _persist(result: BenchResult) -> Path:
    OUT_ROOT.mkdir(parents=True, exist_ok=True)
    out = OUT_ROOT / "lazygraphrag_bench_latest.json"
    out.write_text(json.dumps(result.to_dict(), indent=2), encoding="utf-8")
    return out


# ─── CLI ────────────────────────────────────────────────────────────


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument(
        "--queries", default=str(DEFAULT_QUERIES),
        help=f"query bench JSON (default {DEFAULT_QUERIES})",
    )
    p.add_argument(
        "--top-k", type=int, default=10,
        help="top-K for nDCG (default 10 — matches v20 gate condition #5)",
    )
    p.add_argument(
        "--threshold-pct", type=float, default=15.0,
        help="nDCG@10 uplift % threshold (default 15 — v20 gate condition #5)",
    )
    p.add_argument(
        "--json", action="store_true",
        help="emit the full result JSON to stdout in addition to the snapshot file",
    )
    return p


def main() -> int:
    args = build_parser().parse_args()
    try:
        result = asyncio.run(_run_bench(args))
    except Exception as exc:
        logger.error("bench failed: %s", exc, exc_info=True)
        return 2
    out = _persist(result)
    logger.info(
        "lazygraphrag_bench_latest written: %s "
        "(baseline=%.3f graphrag=%.3f uplift=%.2f%%)",
        out, result.ndcg_baseline_mean, result.ndcg_graphrag_mean,
        result.ndcg_uplift_pct,
    )
    if args.json:
        print(json.dumps(result.to_dict(), indent=2))
    if result.ndcg_uplift_pct >= args.threshold_pct:
        return 0
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
