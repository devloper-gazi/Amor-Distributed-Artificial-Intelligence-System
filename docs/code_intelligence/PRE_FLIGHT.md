# Code Intelligence Mode — Pre-Flight Inventory

**Branch base:** `v2` at commit `d4f48c8` (Code Intelligence v1 — model
registry, sandbox, static analysis, 5 agents, 9-phase engine, 9 routes,
frontend, infra). This document inventories the AMOR codebase the v2
extensions (`RepoMap`, `CapabilityDiscoverer`, `AdversarialReviewer`,
observability, multi-persona Critic, tree-of-thoughts Debugger,
`execute_pytest`) build on.

## Files read (in mandated order)

### 1. `document_processor/api/thinking_routes.py`
Structural template. Exports `router = APIRouter(prefix="/api/thinking")`
with `/analyze`, `/think`, `/{sid}/cancel`, `/{sid}/events` (SSE),
`/{sid}/status`, `/{sid}`. Session storage is a hot `TTLCache(512, 7800)`
fronting Redis (`thinking_session:` prefix, TTL 7200s). SSE uses an
`asyncio.Queue(maxsize=500)` per session with sliding-window drop on
`QueueFull`, plus Redis pub/sub fan-out for cross-replica delivery.
Every event carries `event_id: uuid4().hex` for client-side dedupe.
Cancellation flips `cancel_requested`, persists, and publishes a
`cancelled` event; the engine polls between phases. Background task
launched via `BackgroundTasks.add_task(_run_session, session_id)`.
Persistence happens at terminal status via the shared
`_query_persistence` helpers — same idempotency key as the frontend.
A `sweep_stale_event_queues()` function is hooked into the lifespan
sweeper.
**Public symbols:** `router`, `_event_queues`, `sweep_stale_event_queues`,
`_persist`, `_load`, `_publish`.

### 2. `document_processor/thinking/engine.py`
Engine template. `ThinkingEngine` owns one session for its lifetime.
Six phases (`understand → decompose → explore → evaluate → synthesize →
critique`). Each phase wraps in `_run_phase(name, runner)` which sets
`status="in_progress"`, emits `phase_start`, runs, emits
`phase_complete` with detail OR `phase_failed` with error, and never
lets a phase exception kill the rest of the pipeline. `_emit` swallows
subscriber errors. Token budgets per effort tier (`_EFFORT_BUDGETS`)
+ legacy alias resolution (`quick→basic`, `standard→medium`).
**Engine is LLM-agnostic**: `llm_call` is injected at construction.
JSON parsing uses `_extract_json` with three fallback strategies
(direct → fenced → widest braces + trailing-comma cleanup).
**Public symbols:** `ThinkingEngine`, `ThinkingPhase`, `_extract_json`,
`PHASE_PROGRESS`, `_EFFORT_BUDGETS`.

### 3. `document_processor/thinking/{models,prompts}.py`
Pydantic patterns: `ClarifyingQuestion`, `AnalyzeRequest`,
`AnalyzeResponse`, `ThinkRequest` (carries `chat_session_id`,
`query_record_id`, idempotency keys for the Phase C linkage).
Prompts use `textwrap.dedent` and request **JSON-only output** with
explicit schemas embedded in the prompt body. `SYSTEM_PROMPT` is the
shared persona.

### 4. `document_processor/api/local_ai_routes_simple.py`
`call_ollama(prompt, system, max_tokens)` is the canonical LLM bridge
(line 876). It does Redis cache (opt-in), calls `_call_ollama_uncached`
which talks `POST /api/generate` to Ollama with `temperature=0.7`,
`num_predict=max_tokens`, and a 900s default HTTP timeout. Auto-pulls
the model on 404 via `_ensure_ollama_ready()`. **Constants used:**
`OLLAMA_BASE_URL`, `OLLAMA_MODEL`, `OLLAMA_HTTP_TIMEOUT_SECONDS`,
`_OLLAMA_TEMPERATURE = 0.7`. The thinking routes import `call_ollama`
lazily (`from .local_ai_routes_simple import call_ollama`) to avoid
circular module load.

### 5. `document_processor/api/chat_research_routes.py`
**Forbidden import target.** Contains `anthropic_client` plus the
Claude API path. The new code intelligence files must NEVER import
`anthropic_client`, `anthropic`, or any Claude function.

### 6. `document_processor/api/{chat_sessions,query_record}_routes.py`
- `_normalize_mode()` accepts `"research", "thinking", "coding", "code"`
  (last one added in `d4f48c8`).
- `chat_messages` collection has a unique sparse index on
  `idempotency_key` for defense-in-depth dedupe across frontend/backend
  parallel writes.
- `query_records` collection: `chat_session_id`, `mode`, `status`
  (`pending|running|completed|failed|cancelled`), `progress`,
  `current_phase`, `started_at`, `completed_at`, `error`,
  `result_markdown`, `idempotency_key`.

### 7. `document_processor/infrastructure/cache.py`
`cache_manager.set_json(key, value, ttl)`, `get_json(key)`,
`publish_event(channel, event)`, `subscribe_events(channel)` (async
generator). Pub/sub backed by Redis. `set_json` uses `json.dumps`
with default `str` for `datetime` etc.

### 8. `document_processor/infrastructure/chat_store.py`
`ChatStore.append_message(client_id, user_id, session_id, role,
content, format, ai_type, extras, idempotency_key)`. `_write_with_retry`
wraps every Mongo write with retry-on-network-error. The latest
chat-store `append_message` honours `idempotency_key` via a unique
sparse index that collapses parallel writes from frontend + backend
to a single row. `update_query_record(record_id, **fields)` for
record updates; `get_query_record(record_id)` for reads.

### 9. `document_processor/infrastructure/storage.py`
`StorageManager` holds Mongo + Redis connections. Mongo client uses
`w="majority"`, `journal=True`, `retryWrites=True`, exponential-backoff
connect retry (5 attempts). Re-validates a sticky connection with a
2s `ping` before reuse.

### 10. `document_processor/reliability/{circuit_breaker,retry}.py`
`CircuitBreaker` class with `failure_threshold`, `recovery_timeout_seconds`.
`@retry(max_attempts, exceptions, base_delay)` decorator with exponential
backoff. Both should wrap every Ollama call from new code (the existing
`call_ollama` already chains them implicitly via the route handler).

### 11. `document_processor/main.py`
- Routers registered in order: chat_research, local_ai, auth, thinking,
  chat_sessions, query_record, chat_folders, crawling, translation,
  code_intelligence (added in `d4f48c8`).
- Lifespan startup: pipeline.start, chat_store.ensure_indexes,
  auth_service.bootstrap, initialize_local_ai, `_sse_queue_sweeper`
  task, `_code_intelligence_warmup` task.
- Lifespan shutdown: cancel sweeper.

### 12. `document_processor/config/settings.py`
`Settings(BaseSettings)` with `Config.env_file=".env"`,
`extra="ignore"`. v1 added `code_*` fields covering ollama URL,
sandbox, debug iterations, auto-pull, session TTL, prewarm images.
**This module needs:** `code_capability_discovery_enabled`,
`code_capability_discovery_interval_seconds`,
`code_capability_discovery_max_per_cycle`, `code_langfuse_url`,
`code_langfuse_public_key`, `code_langfuse_secret_key`.

### 13. Frontend (`web_ui/static/js/{app,chat-research,thinking-view}.js`)
- `app.js`: `state.currentMode`, `applyMode(newMode)`,
  `formatModeShort` (`R`/`T`/`C`/`CI`), `updateModeDisplay`. Welcome
  cards rendered from `<div data-mode="...">`.
- `chat-research.js`: `ChatController` class. Runs the per-mode
  pipelines. Each mode has `_runX`, `_streamX`, `_pollX`,
  `_persistActiveX`, `_resumeActiveXIfAny` helpers; v1 added the
  `_runCodeIntelligence` family.
- `thinking-view.js`: View class pattern. `class ThinkingView` with
  `getElement()`, `showTimeline()`, `handleEvent(evt)`,
  `loadFromSnapshot(snap)`, `toSnapshot()`. v1 mirrored this in
  `code-view.js`.

### 14. `web_ui/static/css/chat-research.css`
Token-based styling, dark mode, mobile breakpoints. v1 appended a
~530-line block under "Code Intelligence View".

### 15. `web_ui/templates/index.html`
Loads JS in order: auth, auth-chip, app, research-view, thinking-view,
code-view, chat-research. v1 added the highlight.js CDN + the Code
Intelligence capability card.

### 16. Infra (`docker-compose.yml`, `Dockerfile`, `.env.example`,
`requirements.txt`)
- App service has 2 replicas, gateway fronts on `:8000`.
- v1 added `/var/run/docker.sock:ro` mount + 9 `CODE_*` env vars +
  `pylint`, `bandit`, `radon` to requirements.

## Conventions verified

- All session IDs: `str(uuid4())`.
- All timestamps: `datetime.now(timezone.utc).isoformat()`.
- Every Mongo write: through `_write_with_retry()`.
- Every endpoint: `Depends(get_current_user)` + ownership check
  (HTTPException 404 on mismatch — never 403, to avoid leaking session
  existence).
- SSE: per-session `asyncio.Queue(maxsize=500)` + `TTLCache(512, 7800)`
  on the dict + Redis pub/sub fan-out + per-event `event_id` dedupe.
- Cancellation: `cancel_requested` flag + `status="cancelled"` + SSE
  `cancelled` event + `mark_query_cancelled` query-record update.
- LLM-agnostic engines: inject `llm_call`, never import.
- JSON parsing: `_extract_json` 3-tier fallback.

## Self-review

Re-read after writing this doc — confirmed every pattern listed above
is reflected in the v1 commit (`d4f48c8`). The v2 additions
(`RepoMap`, `CapabilityDiscoverer`, `AdversarialReviewer`, observability
decorator, multi-persona Critic, tree-of-thoughts Debugger,
`execute_pytest`) MUST follow these exact conventions where they
overlap. Discrepancies will be logged in `PROMPT_DEVIATIONS.md`.
