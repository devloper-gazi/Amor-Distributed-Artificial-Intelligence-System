# ADR-0002 — Defer vector store

**Date:** 2026-04-27
**Status:** Accepted
**Context:** Master Prompt §9 default suggests Qdrant or Mongo Atlas Vector Search.

## Decision

Do **not** add a vector store in v2. RepoMap (the v2 workspace map) uses
tree-sitter + NetworkX PageRank, not embeddings. No code path in the
engine queries semantic similarity yet.

## Rationale

- Adds a 200 MB image (Qdrant) or a substantial Mongo Atlas dependency
  for a feature we don't yet exercise.
- The user's machine already runs 11 services.
- When semantic retrieval lands (e.g., a future code-RAG feature), we
  can choose between Qdrant, LanceDB (already in `requirements.txt`),
  or Mongo Atlas Vector Search at that point with full information.

## Consequences

- v2 ships with deterministic structural retrieval only.
- Future Code-RAG work needs its own ADR + a vector-store choice.
- LanceDB is already a runtime dep (used by the research mode); if a
  small embedding need arises before Code-RAG lands, we can prototype
  inside LanceDB without a new service.
