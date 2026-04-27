# Code Intelligence v2 — Session Log

## 2026-04-27 — v2 build session

**Branch:** `feat/code-intelligence-mode-v2` (from `v2` at `d4f48c8`)
**Status:** Pushed, ready for review

### Shipped (9 atomic commits)

1. `da89923` docs(code): pre-flight inventory — PRE_FLIGHT.md +
   PATTERNS.md + INVARIANTS.md + INTEGRATION_MAP.md
2. `a4d5ed1` feat(code): observability — @traced decorator +
   Langfuse/JSONL fallback (5 tests)
3. `54ac04e` feat(code): AdversarialReviewer — synchronous event
   filter + YAML rule pack (11 tests)
4. `54eb6ba` feat(code): RepoMap — tree-sitter + PageRank workspace
   summary (8 tests)
5. `947b123` feat(code): CapabilityDiscoverer — autonomous
   self-extension protocol (18 tests)
6. `6ad9a1b` feat(code): wire v2 modules — adversarial filter,
   /capabilities, lifespan
7. `76e81ea` feat(ui): code-view handles adversarial_alert event + CSS
8. `10cd501` feat(code): infra wiring for v2 — deps + env + compose
9. `d4981be` docs(code): v2 architecture, runbook, changelog,
   capabilities, ADR

### Validation

- 42/42 v2 tests pass (`pytest -q tests/code_intelligence/`)
- 11 `/api/code/*` endpoints registered (was 9 in v1, +
  `/capabilities`, `/capabilities/discover`)
- Zero-API audit clean — no anthropic/openai/cohere/voyageai hits in
  new code or `code-view.js`
- All Python ASTs parse, both modified JS files lint via `node -c`
- Branch pushed; PR URL surfaces from GitHub on push

### Deferred to v2.1 (logged in ADR-0001)

- Tree-of-thoughts Debugger (3-candidate diagnoses + scoring)
- Multi-persona Critic ensemble (Security / Style / Performance /
  Spec → Judge)
- `execute_pytest` structured pytest result wrapper
- `extract_symbol_graph` tree-sitter helper for non-Python languages
- Strict-mode capability discovery sandboxed install + smoke +
  benchmark gates
- Vector store (Qdrant) — not yet needed; RepoMap doesn't use
  embeddings
- FastMCP 2.x integration for the MCP server smoke test

### How to resume

1. Switch to the v2 branch: `git checkout feat/code-intelligence-mode-v2`
2. Pull the latest: `git pull origin feat/code-intelligence-mode-v2`
3. Re-run the test suite: `docker exec amor-app-1 sh -c
   "PYTHONPATH=/app pytest -q tests/code_intelligence/"`
4. Pick a deferred item from ADR-0001 § Decisions or CHANGELOG.md →
   v2.1 candidates
5. Continue the test-first → atomic-commit cadence
