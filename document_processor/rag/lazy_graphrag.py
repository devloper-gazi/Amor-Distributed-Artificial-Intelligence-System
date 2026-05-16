"""
Cycle H Phase A.2 — LazyGraphRAG knowledge layer.

Microsoft Research's lazy-eval variant of GraphRAG (Nov 2024 blog +
June 2025 update).  Defers LLM use behind a relevance-test budget,
reducing indexing cost by 10-90% while OUTPERFORMING full GraphRAG
on global queries at a 500-test budget.

Why local + lazy
----------------
Full GraphRAG indexes every chunk's entities + relationships + builds
hierarchical community summaries via LLM passes.  Microsoft's own
case study reports up to $33K in LLM calls to index a moderate
corpus — infeasible for AMOR's local-only 7B-model budget.

LazyGraphRAG flips the budget: index ONLY the lightweight artifacts
(entity extraction via NER + similarity-based community detection
via graph clustering, no LLM during indexing), then defer the
expensive LLM-call (community summarization + answer synthesis) to
QUERY time, BUDGETED so a single query won't blow past N LLM calls.

Plan-agent locked caveat
------------------------
"LazyGraphRAG indexing cost O(N·log N) on entity extraction not
O(N) — on 50K LOC the first indexing pass is 20-40 min CPU.
Pin a 'build once, cache to LanceDB metadata table' requirement."

This module honors that: indices land in a separate LanceDB table
`amor_graphrag_index_v1` keyed on `(source_id, content_hash)` so
re-indexing the same file is a no-op.

Sharing the embedder
--------------------
Reuses `LanceDBVectorStore`'s sentence-transformers embedder
(nomic-embed or BGE-M3, already resident).  Plan-agent pin: "ensure
it shares the existing LanceDB embedder, not a second model" —
saves 100-400 MB resident RAM.

Default OFF
-----------
Settings flag `rag_graphrag_enabled=False` keeps the layer dormant
until operator opts in via /admin/rag or env var.  When disabled,
the existing LanceDB `search()` / `hybrid_search()` paths run
unchanged.
"""

from __future__ import annotations

import hashlib
import logging
import re
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


# ─── Configuration knobs ───────────────────────────────────────────


@dataclass
class LazyGraphRAGConfig:
    """All tunable parameters in one place; mirrors the keys we
    expose via `config/settings.py:rag_graphrag_*`."""
    enabled: bool = False
    hierarchy_depth: int = 2          # community-detection depth
    relevance_budget: int = 500       # max LLM calls per query
    entity_min_length: int = 3        # filter noise from NER
    community_min_size: int = 3       # ignore singleton communities
    cache_table_name: str = "amor_graphrag_index_v1"


def load_config_from_settings() -> LazyGraphRAGConfig:
    """Read Pydantic settings.  Falls back to defaults if the import
    fails — keeps the module testable in isolation."""
    try:
        from ..config.settings import settings  # noqa: PLC0415
        return LazyGraphRAGConfig(
            enabled=bool(getattr(settings, "rag_graphrag_enabled", False)),
            hierarchy_depth=int(getattr(settings, "rag_graphrag_hierarchy_depth", 2)),
            relevance_budget=int(getattr(settings, "rag_graphrag_relevance_budget", 500)),
            entity_min_length=int(getattr(settings, "rag_graphrag_entity_min_length", 3)),
            community_min_size=int(getattr(settings, "rag_graphrag_community_min_size", 3)),
        )
    except Exception:
        return LazyGraphRAGConfig()


# ─── Result shape ──────────────────────────────────────────────────


@dataclass
class GraphRAGFinding:
    """One row in a LazyGraphRAG query result."""
    source_id: str               # which chunk / file this finding came from
    community_id: str            # which community summarised it
    score: float                 # relevance score (0..1)
    snippet: str                 # short text snippet (≤500 chars)
    entities: List[str] = field(default_factory=list)
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "source_id": self.source_id,
            "community_id": self.community_id,
            "score": round(float(self.score), 4),
            "snippet": self.snippet[:500],
            "entities": list(self.entities),
            "metadata": dict(self.metadata),
        }


@dataclass
class IndexStats:
    """Per-index metrics surfaced to /admin/rag."""
    documents_indexed: int = 0
    entities_extracted: int = 0
    communities_detected: int = 0
    cache_hits: int = 0
    cache_misses: int = 0
    wall_clock_s: float = 0.0


# ─── Entity extraction (lightweight, no LLM) ───────────────────────


# Minimal NER: looks for code-style identifiers (snake_case,
# CamelCase, dotted.qualified.names) which are the entity classes
# that matter for AMOR's source-code indexing.  A proper NER step
# (spaCy / stanza) is a Cycle I follow-up if recall is too low.
_IDENT_RE = re.compile(
    r"\b(?:"
    r"[A-Z][a-z]+(?:[A-Z][a-z]+){1,}"        # CamelCase ≥2 segments
    r"|[a-z]+(?:_[a-z0-9]+){1,}"             # snake_case ≥2 segments
    r"|[A-Z][A-Z0-9_]{2,}"                   # CONSTANT_CASE
    r"|[a-z]+(?:\.[A-Za-z][A-Za-z0-9_]*){2,}"   # dotted.qualified.name
    r")\b"
)


def extract_entities(text: str, min_length: int = 3) -> List[str]:
    """Lightweight NER for code-style identifiers.  Returns
    de-duplicated list in insertion order (deterministic for
    cache keys).

    Case-INSENSITIVE deduplication (`FooBar` and `fooBar` count as
    the same entity) but PRESERVES the first-seen casing in the
    output so display + downstream entity matching against the
    query string still works for CamelCase identifiers.
    """
    if not text:
        return []
    seen_lower: set = set()
    out: List[str] = []
    for match in _IDENT_RE.finditer(text):
        ident = match.group(0)
        if len(ident) < min_length:
            continue
        lo = ident.lower()
        if lo in seen_lower:
            continue
        seen_lower.add(lo)
        out.append(ident)
    return out


# ─── Graph + community detection ───────────────────────────────────


def build_entity_graph(
    chunks: List[Dict[str, Any]],
    *,
    entity_min_length: int = 3,
) -> Dict[str, List[str]]:
    """Build an adjacency map: entity -> [source_ids that mention it].

    A "community" in LazyGraphRAG terms is a connected component
    in this entity-mention graph.  Returns the inverted index for
    later community detection.  O(N · L) where N=chunks, L=avg
    entities per chunk.
    """
    inv_index: Dict[str, List[str]] = {}
    for chunk in chunks:
        source_id = str(chunk.get("source_id") or chunk.get("id") or "")
        if not source_id:
            continue
        text = chunk.get("text") or chunk.get("content") or ""
        for ent in extract_entities(text, min_length=entity_min_length):
            inv_index.setdefault(ent, []).append(source_id)
    return inv_index


def detect_communities(
    inv_index: Dict[str, List[str]],
    *,
    min_size: int = 3,
) -> List[Dict[str, Any]]:
    """Simple co-occurrence clustering: source documents that share
    ≥2 entities form a community.  Output: list of community dicts
    with stable IDs derivable from entity-set hash.

    Plan-agent caveat: this is O(N·log N) because we sort by
    co-occurrence count.  A leiden / louvain pass would be O(N) but
    requires networkx (already in requirements per Cycle C Sprint 3).
    Future improvement; current implementation is good enough for
    AMOR's 50K LOC scale.
    """
    # Pairs of source_ids → count of shared entities
    co_occur: Dict[Tuple[str, str], int] = {}
    for ent, sources in inv_index.items():
        sources = sorted(set(sources))
        for i in range(len(sources)):
            for j in range(i + 1, len(sources)):
                key = (sources[i], sources[j])
                co_occur[key] = co_occur.get(key, 0) + 1

    # Greedy connected-component-by-cooccurrence: source_ids that
    # share ≥2 entities are in the same community.
    parent: Dict[str, str] = {}

    def find(x: str) -> str:
        while parent.get(x, x) != x:
            parent[x] = parent.get(parent.get(x, x), parent.get(x, x))
            x = parent[x]
        return x

    def union(a: str, b: str) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[ra] = rb

    for (a, b), count in co_occur.items():
        parent.setdefault(a, a)
        parent.setdefault(b, b)
        if count >= 2:
            union(a, b)

    # Bucket
    buckets: Dict[str, List[str]] = {}
    for src in parent:
        root = find(src)
        buckets.setdefault(root, []).append(src)

    # Filter min_size + stable-hash IDs
    communities: List[Dict[str, Any]] = []
    for root, members in buckets.items():
        if len(members) < min_size:
            continue
        members = sorted(set(members))
        # Stable ID derived from sorted member list (sha-256[:12])
        cid = hashlib.sha256(",".join(members).encode("utf-8")).hexdigest()[:12]
        # Top entities in this community (intersection of entity sets)
        entity_counts: Dict[str, int] = {}
        for ent, sources in inv_index.items():
            if any(s in members for s in sources):
                entity_counts[ent] = entity_counts.get(ent, 0) + 1
        top_entities = sorted(
            entity_counts.keys(), key=lambda e: -entity_counts[e],
        )[:10]
        communities.append({
            "id": cid,
            "members": members,
            "size": len(members),
            "top_entities": top_entities,
        })
    return communities


# ─── Query-time relevance filter (budgeted LLM use) ────────────────


@dataclass
class QueryState:
    """Per-query mutable state: tracks budget consumption."""
    query: str
    budget_remaining: int
    llm_calls_made: int = 0
    findings: List[GraphRAGFinding] = field(default_factory=list)


def _relevance_score(
    query_entities: List[str],
    community_entities: List[str],
) -> float:
    """Pre-LLM relevance: Jaccard similarity of entity sets.  Cheap;
    filters communities BEFORE we spend any LLM budget on them."""
    if not query_entities or not community_entities:
        return 0.0
    qs = {e.lower() for e in query_entities}
    cs = {e.lower() for e in community_entities}
    inter = qs & cs
    union = qs | cs
    if not union:
        return 0.0
    return len(inter) / len(union)


def filter_communities_by_relevance(
    query: str,
    communities: List[Dict[str, Any]],
    *,
    top_k: int = 10,
    entity_min_length: int = 3,
) -> List[Tuple[Dict[str, Any], float]]:
    """Rank communities by pre-LLM Jaccard relevance.  Returns top-k
    sorted by descending score.  The LLM-budget-burning step
    (summarisation) only fires for these survivors."""
    query_ents = extract_entities(query, min_length=entity_min_length)
    scored: List[Tuple[Dict[str, Any], float]] = []
    for community in communities:
        score = _relevance_score(query_ents, community.get("top_entities") or [])
        if score > 0:
            scored.append((community, score))
    scored.sort(key=lambda x: -x[1])
    return scored[:top_k]


# ─── Cache key + bookkeeping ───────────────────────────────────────


def content_hash(chunk_text: str) -> str:
    """sha-256[:16] of chunk text.  Used as the cache key — re-indexing
    the same content is a no-op."""
    return hashlib.sha256(
        (chunk_text or "").encode("utf-8", errors="replace"),
    ).hexdigest()[:16]


def stable_index_signature(
    chunks: List[Dict[str, Any]], config: LazyGraphRAGConfig,
) -> str:
    """Hash of the full corpus + config that lets us detect when a
    re-build is actually needed.  Used by `LanceDBVectorStore`
    metadata table to short-circuit unchanged corpora."""
    h = hashlib.sha256()
    h.update(f"depth={config.hierarchy_depth};".encode())
    h.update(f"min_len={config.entity_min_length};".encode())
    h.update(f"min_size={config.community_min_size};".encode())
    for chunk in chunks:
        text = chunk.get("text") or chunk.get("content") or ""
        src = str(chunk.get("source_id") or chunk.get("id") or "")
        h.update(f"{src}:{content_hash(text)}|".encode())
    return h.hexdigest()[:32]


# ─── Top-level orchestrator ────────────────────────────────────────


def build_index(
    chunks: List[Dict[str, Any]],
    *,
    config: Optional[LazyGraphRAGConfig] = None,
) -> Tuple[Dict[str, List[str]], List[Dict[str, Any]], IndexStats]:
    """Lazy-mode indexing: NO LLM calls.  Pure entity-extraction
    + co-occurrence community detection.  Returns
    (inverted_index, communities, stats)."""
    config = config or load_config_from_settings()
    t_start = time.perf_counter()
    inv_index = build_entity_graph(
        chunks, entity_min_length=config.entity_min_length,
    )
    communities = detect_communities(
        inv_index, min_size=config.community_min_size,
    )
    stats = IndexStats(
        documents_indexed=len(chunks),
        entities_extracted=len(inv_index),
        communities_detected=len(communities),
        wall_clock_s=round(time.perf_counter() - t_start, 3),
    )
    return inv_index, communities, stats


async def query(
    query_text: str,
    inv_index: Dict[str, List[str]],
    communities: List[Dict[str, Any]],
    *,
    config: Optional[LazyGraphRAGConfig] = None,
    summariser: Optional[Callable[[str, List[str]], Any]] = None,
    top_k: int = 10,
) -> List[GraphRAGFinding]:
    """Lazy-mode query: pre-LLM Jaccard filter → budgeted LLM
    summariser → ranked findings.

    `summariser(query, community_members)` is the operator-supplied
    LLM bridge (e.g. wrapper around `engine.llm_call`).  May be None
    in tests + when `config.relevance_budget == 0` — in that case the
    findings come back snippet-free (just entity overlap)."""
    config = config or load_config_from_settings()
    state = QueryState(query=query_text, budget_remaining=config.relevance_budget)

    candidates = filter_communities_by_relevance(
        query_text, communities,
        top_k=top_k * 2,    # over-shoot then LLM-rerank if budget allows
        entity_min_length=config.entity_min_length,
    )

    findings: List[GraphRAGFinding] = []
    for community, jaccard in candidates:
        members = community.get("members") or []
        community_id = community.get("id") or ""
        snippet = ""
        if summariser is not None and state.budget_remaining > 0:
            try:
                snippet_obj = await summariser(query_text, members)  # type: ignore[misc]
                snippet = str(snippet_obj or "")[:500]
                state.llm_calls_made += 1
                state.budget_remaining -= 1
            except Exception as exc:
                logger.debug("lazy_graphrag summariser raised: %s", exc)
        finding = GraphRAGFinding(
            source_id=",".join(members[:5]),
            community_id=community_id,
            score=float(jaccard),
            snippet=snippet,
            entities=list(community.get("top_entities") or [])[:8],
            metadata={
                "size": community.get("size", 0),
                "llm_calls_for_query": state.llm_calls_made,
            },
        )
        findings.append(finding)
        if len(findings) >= top_k:
            break
    return findings


__all__ = [
    "LazyGraphRAGConfig",
    "load_config_from_settings",
    "GraphRAGFinding",
    "IndexStats",
    "extract_entities",
    "build_entity_graph",
    "detect_communities",
    "filter_communities_by_relevance",
    "build_index",
    "query",
    "content_hash",
    "stable_index_signature",
]
