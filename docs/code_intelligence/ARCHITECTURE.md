# Code Intelligence Mode — Architecture

A first-class mode in the AMOR Distributed AI System for code
generation, debugging, review, refactoring and technical assistance —
running 100% on local Ollama models with zero external API cost.

## Layered view

```
┌── UI surface ──────────────────────────────────────────────────────┐
│ web_ui/static/js/code-view.js (CodeView)                           │
│ web_ui/static/js/chat-research.js (_runCodeIntelligence)           │
│ web_ui/static/js/app.js (mode button + resume)                     │
└────────────────────────────────────────────────────────────────────┘
┌── HTTP / SSE surface ──────────────────────────────────────────────┐
│ document_processor/api/code_intelligence_routes.py                 │
│ POST   /api/code/triage                                            │
│ POST   /api/code/start                                             │
│ GET    /api/code/{sid}/events     (SSE)                            │
│ GET    /api/code/{sid}/status                                      │
│ GET    /api/code/{sid}                                             │
│ POST   /api/code/{sid}/cancel                                      │
│ GET    /api/code/models                                            │
│ POST   /api/code/models/{tag}/pull (SSE)                           │
│ GET    /api/code/sandbox/health                                    │
│ GET    /api/code/capabilities          (v2)                        │
│ POST   /api/code/capabilities/discover (v2)                        │
└────────────────────────────────────────────────────────────────────┘
┌── Orchestration ───────────────────────────────────────────────────┐
│ CodeIntelligenceEngine (LLM-agnostic)                              │
│ 9 phases: triage → model_prep → plan → implement → execute →       │
│ analyze → test → debug → review                                    │
│ Debug loop with max_debug_iterations cap (3 default, 5 expert)     │
└────────────────────────────────────────────────────────────────────┘
┌── Agents ──────────────────────────────────────────────────────────┐
│ Planner   : task classification + dependency-ordered plan          │
│ Coder     : full implementation, fenced code + JSON metadata       │
│ Tester    : idiomatic test framework, edge cases, security         │
│ Debugger  : root-cause + minimal fix from real exec feedback       │
│ Critic    : verdict, score, issues, security/perf concerns         │
└────────────────────────────────────────────────────────────────────┘
┌── Capabilities ────────────────────────────────────────────────────┐
│ ModelRegistry        — 12 catalogued Ollama tags + auto-pull       │
│ ExecutionSandbox     — Docker --network none --read-only --tmpfs   │
│ StaticAnalysisHarness— AST + pylint + mypy + bandit + radon        │
│ RepoMap              (v2) — tree-sitter PageRank workspace summary │
│ AdversarialReviewer  (v2) — synchronous SSE event filter           │
│ CapabilityDiscoverer (v2) — autonomous self-extension (HF/GH/arXiv)│
│ CapabilityRegistry   (v2) — Mongo-backed, in-process fallback      │
│ Observability        (v2) — @traced + Langfuse / JSONL fallback    │
└────────────────────────────────────────────────────────────────────┘
┌── Existing infrastructure ─────────────────────────────────────────┐
│ Ollama · MongoDB · Redis · Docker · CircuitBreaker · @retry        │
└────────────────────────────────────────────────────────────────────┘
```

## Per-session lifecycle

1. POST `/api/code/start` — spawn UUID4, persist to Redis (key prefix
   `code_session:`, TTL 7200s), return immediately.
2. `_run_session` background task instantiates `CodeIntelligenceEngine`
   with injected `_llm_call_local` (the only LLM bridge).
3. Engine runs the 9 phases. Each phase emits `phase_start` /
   `phase_complete` (or `phase_failed`) events. Every event is
   `event_id`-stamped + queued + Redis-pubsub-fanned-out + filtered
   through `AdversarialReviewer`.
4. On code generation, `RepoMap.repo_map(focus_files=[…])` is prepended
   to the Coder/Debugger/Critic prompts so they see current workspace
   structure (~1024 token budget; binary-search fitter).
5. After `execute`, if the sandbox returns failure → debug loop runs
   until success or `max_debug_iterations`. Each iteration: Debugger
   produces a fix → re-execute → either succeed or loop.
6. On terminal status (completed / failed / cancelled), the helpers
   `persist_user_message`, `persist_assistant_message`,
   `mark_query_completed/failed/cancelled` write to MongoDB with the
   shared idempotency keys (defense-in-depth dedupe with frontend).

## v2 cross-cuts

- **Adversarial filter on `_publish`**: every event passes through
  `AdversarialReviewer.inspect_event()` before queueing. Critical
  matches → original event suppressed, alert published, session
  flagged for cancellation.
- **Capability discovery in lifespan**: `run_forever()` task spawned at
  startup; sleeps `interval_seconds` between cycles. Cancelled
  cleanly on shutdown.
- **Tracing**: every async function wrapping an LLM call, sandbox
  execution, registry pull, or capability gate is decorated with
  `@traced(role=...)`. Spans accumulate in Langfuse OR JSONL.

## Failure-quiet by design

Every external dependency (Langfuse SDK, tree-sitter, NetworkX, HF
Hub, PyGithub, arxiv, Mongo) is imported lazily and the calling code
falls through cleanly when the import fails. The system stays
functional in offline environments — the missing capability simply
isn't available, never crashes the engine.
