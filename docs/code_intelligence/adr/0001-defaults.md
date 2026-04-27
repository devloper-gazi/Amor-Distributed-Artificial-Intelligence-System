# ADR-0001 — v2 default technology choices

**Date:** 2026-04-27
**Status:** Accepted
**Branch:** `feat/code-intelligence-mode-v2`

## Context

Building Code Intelligence v2 required half a dozen decisions where the
master prompt allowed several reasonable defaults. This ADR records
the choices made.

## Decisions

### 1. Vector store — defer

The master prompt suggests Qdrant or Mongo Atlas Vector Search. The v2
build does NOT yet need a vector store — RepoMap uses tree-sitter +
PageRank, not embeddings. We keep the option open by NOT introducing a
vector dep, so the future addition stays a clean PR.

Alternative considered: pre-add Qdrant as a service. **Rejected**
because it's a 200 MB image we don't yet use and the user's machine
already runs 11 services.

### 2. Reranker — none

No reranker is currently invoked. When the future RAG extension lands,
we'll evaluate `BAAI/bge-reranker-v2-m3` via Ollama or a sidecar at
that point.

### 3. Background scheduler — bare asyncio

`CapabilityDiscoverer.run_forever()` is a plain asyncio task spawned
from the FastAPI lifespan. No Taskiq / Celery introduced.

Alternative considered: Taskiq (the prompt's default). **Rejected for
v2** because the discoverer is single-instance per process and lifespan
already gives us clean cancellation. Taskiq would add a Redis broker
config + worker container without a current gain.

### 4. Observability — Langfuse OPTIONAL, JSONL fallback

The `@traced` decorator tries Langfuse only when all three env vars
(`CODE_LANGFUSE_URL`, `CODE_LANGFUSE_PUBLIC_KEY`,
`CODE_LANGFUSE_SECRET_KEY`) are set AND the `langfuse` package is
importable. Default → JSONL trace files under
`document_processor/code_intelligence/traces/`.

Alternative considered: pre-include Langfuse in `requirements.txt`.
**Rejected** to keep the install footprint small. JSONL is enough for
local-only debugging.

### 5. MCP server framework — none yet

The Capability Discoverer's MCP-server smoke test (master prompt §4.8
gate 5) is **deferred** in non-strict mode. When strict mode is wired
up, FastMCP 2.x is the chosen client. For v2, the discoverer logs MCP
server candidates without invoking them.

### 6. Tree-of-thoughts Debugger / multi-persona Critic ensemble

The master prompt §4.6 calls for a tree-of-thoughts Debugger (3
candidate diagnoses, scored, best pursued) and a 4-persona Critic
ensemble (Security / Style / Performance / Spec → Judge merger).

These extensions are **deferred to v2.1**. The v1 Debugger and Critic
remain as-is. Reason: the existing single-pass agents already produce
reasonable output on small tasks; the ensemble cost (5× LLM calls per
review) is significant on local Ollama and benefits aren't yet
measurable without an eval harness in place.

### 7. `execute_with_files` / `execute_pytest` — deferred

Master prompt §4.3 extension. The current `execute()` accepts
`extra_files` so multi-file projects already work via the v1 sandbox.
The structured pytest result wrapper is a clear next step but would
also benefit from the eval harness landing first.

## Consequences

- v2 is **shippable as-is** with a smaller install footprint and no
  new infrastructure dependencies.
- The deferred items (vector store, ToT Debugger, multi-persona
  Critic, structured pytest) are tracked in CHANGELOG.md as v2.1
  candidates.
- All deferrals respect the master prompt's invariants (no paid APIs,
  permissive licensing, sandbox containment, existing-codebase
  invariants). They simply postpone optional sophistication.
