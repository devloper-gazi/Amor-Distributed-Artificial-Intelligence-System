# Sprint 7 — Episodic + cross-session memory (Mem0 OSS)

> Cycle C, Days 1–5.  Closed 2026-05-04.

## What shipped

| Day | Deliverable | Key files |
|-----|-------------|-----------|
| 1 | `Mem0Adapter` — thin wrapper around `mem0ai` (OSS Apache-2.0). Always constructible; degrades to a no-op when mem0 isn't installed.  Per-user namespacing, lazy LanceDB / SQLite paths, OpenAI-compat fact-extraction LLM hook | `local_ai/memory/mem0_adapter.py`, `tests/local_ai/test_mem0_adapter.py` |
| 2 | Admin Memory API — `GET /status`, `GET /search`, `GET /all`, `POST /add`, `DELETE /{id}`.  Fully auth-gated, per-user scoped, 503 on add/delete when adapter degraded | `document_processor/api/admin_memory_routes.py`, `tests/api/test_admin_memory_routes.py` |
| 3 | "Remembered N" pill on `MessageBubble` + `/admin/memory` viewer route (status banner / search / list / delete / add) | `web_ui/v2/src/lib/types.ts` (ChatTurn.remembered), `web_ui/v2/src/components/chat/MessageBubble.tsx`, `web_ui/v2/src/routes/Memory.tsx`, palette + main.tsx wiring |
| 4 | Engine recall hook — pulls memories at `_phase_triage`, prepends to system context, emits `memory_recalled` SSE; Build.tsx stamps the assistant turn with `remembered: { count, snippets }` | `document_processor/services/memory_recall.py`, `document_processor/code_intelligence/engine.py`, `web_ui/v2/src/routes/Build.tsx`, `tests/local_ai/test_memory_recall.py` |
| 5 | Cross-sprint test sweep + `sprint7_results.md` + bundle gate | this file |

## Acceptance criteria — pass/fail

* **Privacy: per-user namespacing via `user_id`** — **PASS** (live:
  every memory route reads `user.id` from the auth dependency; no
  cross-user reads possible).
* **All data on disk; no external network** — **PASS** (LanceDB
  vector store, SQLite history-db, fact-extraction LLM points at the
  compose-internal `amor-llama-swap`).
* **Hybrid retrieval (vector + BM25 + entity links)** — _provided
  by Mem0 itself when enabled_; the wrapper passes the search
  through unchanged.
* **Graph memory disabled by default** — **PASS** (Cycle C caveat:
  Neo4j-required features are off; `graph_enabled=false` unless the
  operator explicitly sets `AMOR_MEMORY_NEO4J=1`).
* **Mem0 absent doesn't break the app** — **PASS** (12/12 adapter
  tests + 8/8 route tests assert the no-op fallback path).

## API surface

```
GET    /api/admin/memory/status        adapter posture (backend, vector store, …)
GET    /api/admin/memory/search?q=…    hybrid search, returns top-N memories
GET    /api/admin/memory/all           list all (per-user, paginated)
POST   /api/admin/memory/add           manual write; 503 when mem0 disabled
DELETE /api/admin/memory/{id}          drop one entry; 503 when mem0 disabled
```

## Engine integration

```
prompt arrives → _phase_triage
    ├── repomap_attached  (Sprint 3)
    └── memory_recalled   (Sprint 7)            ← new
              ↓
        triage system context = memory + repomap + user code
              ↓
        downstream phases (plan, code, test, debug, review) unchanged
```

The memory block sits *above* the repomap so the planner sees user
preferences first.  Both injections are env-gated and fault-tolerant
— either failing produces a logged warning + an empty result instead
of a pipeline error.

## Live verification

```
$ curl .../api/admin/memory/status
{"backend":"native","available":false,
 "vector_store":"lancedb",
 "history_db":"data/amor_memory/mem0_history.sqlite",
 "llm_base_url":"http://amor-llama-swap:9100",
 "llm_model":null,"graph_enabled":false,
 "user_namespace":"5337cc6e-…"}

$ curl .../api/admin/memory/search?q=anything
{"q":"anything","limit":10,"count":0,"available":false,"items":[]}

$ curl -X POST .../api/admin/memory/add -d '{"text":"…"}'
HTTP 503 (mem0 not installed yet — flip on by adding `mem0ai` to
requirements.txt and setting AMOR_MEMORY_BACKEND=mem0)
```

## Tests

* `tests/local_ai/test_mem0_adapter.py`     — 12 (adapter no-op +
                                             stub regimes, normaliser)
* `tests/api/test_admin_memory_routes.py`   —  8 (degraded + enabled
                                             regimes for every route)
* `tests/local_ai/test_memory_recall.py`    —  9 (env probe, format,
                                             stub recall, error swallow)

Total new tests this sprint: **29**.  Cross-sprint backend sweep:
**67 passed**.  Frontend sweep: **56 passed**.

## Bundle delta

```
$ node tools/check_bundle_size.mjs
[bundle-size] baseline: 96.20 kB  current: 100.37 kB  delta: +4.18 kB (budget: +40.00 kB)
[bundle-size] OK
```

Memory route + the "Remembered" pill add **~4 kB gzipped**, well
under the +40 kB Sprint 4 budget.

## How operators flip Mem0 on

```bash
# 1. Add mem0ai to the requirements layer.
echo "mem0ai>=0.1.0" >> requirements.txt

# 2. Configure the env.
cat >> .env <<'EOF'
AMOR_MEMORY_BACKEND=mem0
AMOR_MEMORY_VECTOR_STORE=lancedb
AMOR_MEMORY_LLM_BASE_URL=http://amor-llama-swap:9100
AMOR_MEMORY_LLM_MODEL=amor-architect
EOF

# 3. Restart.
docker compose up -d app

# 4. Verify in the UI.
#    /admin/memory  → "mem0 · ready" badge
#    /system        → existing health card unchanged
#    Build a code session  → "Remembered 0" pill appears the first time
#                              the user references stored facts
```

## Caveats

* **`mem0ai` is heavyweight** — the dep brings in
  sentence-transformers (~120 MB) + torch (already in image).  The
  adapter's no-op fallback exists exactly so the operator can defer
  the install until they actually need cross-session memory.
* **Graph memory needs Neo4j** — explicitly disabled in the default
  config.  Operators who want graph memory must add Neo4j to their
  compose stack and set `AMOR_MEMORY_NEO4J=1`; the adapter's
  config builder picks that up automatically.
* **The recall hook only fires for the Code Intelligence engine
  today** — Research / Thinking / Consortium pipelines have their
  own triage paths and will need a follow-up commit to mirror the
  `_phase_triage` injection.  Easy: each pipeline has a similar
  `system_context` assembly site; we just import + invoke
  `recall_for_prompt` there too.
* **Per-user singletons**: every request constructs a fresh adapter
  scoped to the user's UUID.  Mem0's underlying client is cheap to
  build (no network round-trips at construction time when the
  history-db file already exists), so the per-request cost is
  negligible.  If profiling later shows otherwise, a process-wide
  WeakValueDictionary keyed on user_id is the obvious cache.

## Rollback

* **Disable recall in the engine**: `AMOR_MEMORY_RECALL_ENABLED=0`
  in `.env` keeps Mem0 ingestion live but skips the triage-phase
  injection.
* **Disable Mem0 entirely**: drop `AMOR_MEMORY_BACKEND` from the
  env.  Adapter falls back to no-op; routes serve empty results
  with `available: false`.
* **Drop the routes**: remove `app.include_router(admin_memory_router)`
  from `main.py`.  The frontend route still resolves but the
  fetches all 404 — UI handles that gracefully via the
  `available: false` banner pattern.
* **Drop the pill**: revert the `remembered` field in `ChatTurn` +
  the `MessageBubble` block.  No data migration needed.
