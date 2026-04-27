# ADR-0003 — No reranker yet

**Date:** 2026-04-27
**Status:** Accepted
**Context:** Master Prompt §9 default suggests `BAAI/bge-reranker-v2-m3`.

## Decision

Do **not** add a reranker in v2.

## Rationale

The reranker is only useful when a retrieval step produces a candidate
list. v2 has no semantic retrieval (see ADR-0002) — there is nothing
to rerank.

## Consequences

- When Code-RAG lands and produces a candidate list, evaluate
  `BAAI/bge-reranker-v2-m3` (Apache-2.0, runs on Ollama or as a
  sidecar) at that point.
- If a non-RAG path later needs a reranker (e.g., picking among
  3 ToT-Debugger candidate diagnoses), reconsider then.
