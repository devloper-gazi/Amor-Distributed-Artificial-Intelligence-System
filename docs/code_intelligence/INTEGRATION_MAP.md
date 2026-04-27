# Integration Map — Code Intelligence Mode v2

```
                                 ┌──────────────────────────────────────────────┐
                                 │  Browser — code-view.js + chat-research.js   │
                                 │  Receives: snapshot, phase_*, exec_result,   │
                                 │  static_analysis_result, debug_iter_*,       │
                                 │  review_ready, model_download_*, code_ready, │
                                 │  test_ready, adversarial_alert, cancelled    │
                                 └────────────────────┬─────────────────────────┘
                                                      │ SSE (event_id-deduped)
                                 ┌────────────────────▼─────────────────────────┐
                                 │  /api/code/* routes (FastAPI APIRouter)      │
                                 │  9 endpoints + 2 new (capabilities, discover)│
                                 └────────────────────┬─────────────────────────┘
                                                      │
                          ┌───────────────────────────┼───────────────────────────┐
                          │                           │                           │
                          ▼                           ▼                           ▼
                 ┌─────────────────┐        ┌─────────────────┐         ┌─────────────────┐
                 │ CodeIntelligence│        │ Capability      │         │ Adversarial     │
                 │ Engine          │        │ Discoverer      │         │ Reviewer        │
                 │ (9-phase loop)  │        │ (out-of-band)   │         │ (event filter)  │
                 └────────┬────────┘        └────────┬────────┘         └────────┬────────┘
                          │                           │                           │
              ┌───────────┼───────────┐               │                           │
              ▼           ▼           ▼               ▼                           ▼
     ┌─────────────┐ ┌──────────┐ ┌─────────┐ ┌─────────────────┐         ┌──────────────┐
     │ 5 Agents    │ │ Sandbox  │ │ Static  │ │ 6-Gate pipeline:│         │ adversary_   │
     │ Planner     │ │ Docker   │ │ Analysis│ │ • License       │         │ rules.yaml   │
     │ Coder       │ │ run      │ │ AST +   │ │ • Metadata      │         │ + halt via   │
     │ Tester      │ │ --network│ │ pylint  │ │ • Sandbox inst  │         │ cancel flag  │
     │ Debugger    │ │ none +   │ │ + mypy  │ │ • Smoke test    │         └──────────────┘
     │ Critic      │ │ readonly │ │ + bandit│ │ • Benchmark     │
     │ (×4 persona │ │ + memlim │ │ + radon │ │ • Registration  │
     │  ensemble)  │ │ + tmpfs  │ │ + tree- │ └─────────────────┘
     └──────┬──────┘ └────┬─────┘ │ sitter  │
            │             │       │ symbol  │
            │             │       │ graph   │
            │             │       └─────────┘
            ▼             │
     ┌─────────────┐      │
     │  RepoMap    │      │
     │  tree-sitter│      │
     │  +PageRank  │      │
     │  binary-cut │      │
     │  budget     │      │
     └──────┬──────┘      │
            │             │
            └──┬──────────┘
               ▼
     ┌──────────────────────────────────────────┐
     │ call_ollama() — local_ai_routes_simple   │
     │ (only LLM bridge; injected at engine     │
     │ construction, never imported in agents)  │
     └──────────────┬───────────────────────────┘
                    │
                    ▼
     ┌──────────────────────────────────────────┐
     │ Existing infra: Ollama · Mongo · Redis · │
     │ Docker · CircuitBreaker · @retry         │
     └──────────────────────────────────────────┘

     ┌──────────────────────────────────────────┐
     │ @traced decorator (observability.py)     │
     │ wraps every agent.__call__, sandbox      │
     │ exec, model pull, capability gate        │
     │ → OpenLLMetry → Langfuse OR JSONL        │
     │   under traces/                          │
     └──────────────────────────────────────────┘
```

## Cross-cutting

- **Persistence**: Engine writes terminal session state to Redis
  (`code_session:{sid}`) + Mongo (chat_messages, query_records) via
  `_query_persistence` helpers, mirroring the thinking/research path.
- **Cancellation**: Stop button → `/api/code/{sid}/cancel` → flips
  `cancel_requested` + `status="cancelled"` + publishes event +
  `mark_query_cancelled`. Engine polls between phases or catches
  `asyncio.CancelledError`.
- **Resume**: localStorage `amor.activeCode` → on page reload, the
  frontend probes `/api/code/{sid}/status` and re-attaches SSE if the
  session is still in-progress.
- **Capability discovery**: Out-of-band lifespan task. Discovered
  capabilities are written to MongoDB collection `capabilities`,
  hot-loaded into the agent toolbelt on next session, and surfaced
  via `GET /api/code/capabilities`.
- **Adversarial Reviewer**: Synchronous filter on `_publish`. Inspects
  every event payload (especially `code_ready`, `test_ready`,
  `execution_result`) for prompt-injection / secret leakage / shell
  injection patterns. On match, emits `adversarial_alert` and flips
  `cancel_requested`.
- **Observability**: Every async function decorated with `@traced`
  emits an OpenLLMetry span. If `code_langfuse_url` is set, spans
  go to Langfuse; otherwise they're appended as JSON lines to
  `document_processor/code_intelligence/traces/{date}.jsonl`.
