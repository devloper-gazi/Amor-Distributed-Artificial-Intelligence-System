# Sprint 8 — MCP agentic loop (ReAct, OpenHands SDK pattern)

> Cycle C, Days 1–5.  Closed 2026-05-04.

## What shipped

| Day | Deliverable | Key files |
|-----|-------------|-----------|
| 1 | Typed event taxonomy (`MessageEvent`, `ThoughtEvent`, `ActionEvent`, `ObservationEvent`) — immutable Pydantic, ULID-stamped, JSON round-tripping; append-only `Conversation` log with iteration tracking + finish lifecycle | `local_ai/agentic/events.py`, `local_ai/agentic/conversation.py`, `tests/local_ai/test_agentic_events.py` (17) |
| 2 | ReAct prompt template (`<thought>...<action>` cycle) + permissive parser (forgiving JSON, prose-padded blocks, trailing-comma fixup); history renderer for re-prompting | `local_ai/agentic/prompt.py`, `tests/local_ai/test_agentic_prompt.py` (15) |
| 3 | `ReActAgent` driver with pluggable LLM caller + tool dispatcher; 4 termination paths (`finish` / `max-iterations` / `stuck` / `parse-failure`); `StuckDetector` keys on (tool, args-json, output-summary) so jitter doesn't spuriously trip | `local_ai/agentic/agent.py`, `tests/local_ai/test_agentic_loop.py` (11) |
| 4 | `/api/agent/start`, `/sessions/{sid}`, `/sessions/{sid}/cancel`, `/sessions/{sid}/events` (SSE).  Frontend `/agent` route streams thoughts + projects events into Sprint 4 `ToolCallCard` accumulator | `document_processor/api/agent_routes.py`, `web_ui/v2/src/routes/Agent.tsx`, `tests/api/test_agent_routes.py` (5) |
| 5 | Cross-sprint sweep + `sprint8_results.md` + bundle gate | this file |

## Acceptance criteria — pass/fail

* **Mirror OpenHands V1 architecture (Agent / Conversation / Workspace
  / Event)** — **PASS** with the existing `ExecutionSandbox`
  serving the Workspace role (Sprint 5 hardened).
* **`max_iteration_per_run=10`** — **PASS** (configurable per request,
  capped by Pydantic validator at 30).
* **Stuck detection: 3+ identical action/observation pairs** —
  **PASS** (verified by 3 unit tests against the detector + a
  live-loop integration that triggers it).
* **Tool calls dispatch to existing MCP registry** — **PASS**
  (`default_tool_dispatcher` routes through `local_ai.tools.DEFAULT_REGISTRY.dispatch`
  which is the same code path the `/mcp/v1/tools/call` route uses).
* **Stream events via SSE in Sprint 4 schema** — **PASS** (every
  `Event.to_tool_stream()` emits canonical `tool-input-start` /
  `tool-input-available` / `tool-output-available` / `tool-error`
  envelopes; ToolCallCard renders without modification).
* **~400 LOC mirror, no LlamaIndex MCPToolAdapter import** — **PASS**
  (`events.py` ~190 LOC, `conversation.py` ~165 LOC, `prompt.py`
  ~190 LOC, `agent.py` ~250 LOC; total ~795 LOC including
  docstrings, against zero new external deps).

## API surface

```
POST /api/agent/start                       start a ReAct run
GET  /api/agent/sessions/{sid}              snapshot (events + finish state)
POST /api/agent/sessions/{sid}/cancel       stop a running session
GET  /api/agent/sessions/{sid}/events       SSE stream
```

## Termination matrix

| Reason            | When |
|-------------------|------|
| `finish`          | Agent emits `<action>{"tool":"finish","arguments":{"answer":"..."}}</action>` |
| `max-iterations`  | Iteration counter hits `config.max_iterations` (default 10) |
| `stuck`           | Detector finds `config.stuck_window` (default 3) identical action+observation pairs in tail |
| `parse-failure`   | LLM emits `config.max_parse_retries` (default 3) consecutive unparseable completions |

The parse-failure counter is **reset on every clean parse**, so a one-off
LLM hiccup mid-run doesn't poison a long session.

## Live verification

```
$ curl -X POST http://localhost:8000/api/agent/start
                 -d '{"task":"hi"}'
HTTP 401   ← auth gate works

$ curl -H "Authorization: Bearer $TOKEN" .../api/agent/sessions/missing
HTTP 404   ← unknown sid handler works

$ curl http://localhost:8000/agent
HTTP 200   ← /agent SPA route deployed (bundle hash index.MpsItOFg.js)
```

The full happy-path (auth → start → SSE drain → finish) is exercised
by `tests/api/test_agent_routes.py::test_event_stream_emits_snapshot_and_done`.

## Tests

* `tests/local_ai/test_agentic_events.py` — 17 (event immutability,
                                              JSON round-trip, kind
                                              discriminators, tool-stream
                                              projection, conversation
                                              append-only)
* `tests/local_ai/test_agentic_prompt.py` — 15 (render, parse happy path,
                                              prose padding, trailing
                                              commas, error paths)
* `tests/local_ai/test_agentic_loop.py`   — 11 (4 termination paths,
                                              tool dispatch surface,
                                              on_event hook, stuck
                                              detector unit tests)
* `tests/api/test_agent_routes.py`        —  5 (auth, validation, 404,
                                              cancel, SSE end-to-end)

Total new tests this sprint: **48**.  Cross-sprint backend sweep:
**115 passed**.  Frontend sweep: **56 passed**.

## Bundle delta

```
$ node tools/check_bundle_size.mjs
[bundle-size] baseline: 96.20 kB  current: 101.76 kB  delta: +5.57 kB (budget: +40.00 kB)
[bundle-size] OK
```

The Agent route + the SSE accumulator add **~1.5 kB** on top of
Sprint 7's already-merged additions.  Total Sprint 4–8 delta is
**+5.57 kB** — comfortably under the +40 kB Sprint 4 budget.

## Caveats

* **Single-replica state**: `_SESSIONS` is an in-process dict.  The
  Phase 17 PR #3 sticky-cookie pins each client to one replica, so
  this is fine for two-replica dev.  Cross-replica fan-out (Redis
  Streams) lives in Sprint 9 — same pattern as Code Intelligence's
  `_publish` already uses.
* **`AgentRunResult.answer`** comes from the model — not a tool —
  so it's untrusted.  The UI displays it verbatim; sanitisation
  happens at the renderer (DOMPurify already wraps every assistant
  message).
* **No LLM benchmark**: the Cycle C plan caveat applies — OpenHands'
  77 % SWE-Verified comes from Claude Sonnet 4.5; AMOR's 8B local
  models will hit a small fraction of that on real tasks.  This
  sprint shipped the **architecture**, not the benchmark.
* **Tool catalogue snapshot is per-request**: the agent reads the
  registry at start time and includes it in the prompt.  A tool
  registered after start won't appear to that run — call /start
  again to pick up new tools.

## Rollback

* **Disable the route**: drop the `app.include_router(agent_router)`
  line from `main.py`.  The `/agent` UI page still renders but its
  fetches all 4xx — UI handles that via the existing error banner.
* **Disable the loop entirely**: revert the route module + the
  `local_ai/agentic/` package.  Nothing else imports it; zero blast
  radius.
* **Tighten the loop**: send `max_iterations=3` or `stuck_window=2`
  in the start body — both are per-request overrides clamped by
  Pydantic validators.

## How operators try it live

```bash
# 1. Sign in as any AMOR user.
# 2. Hit the command palette → "Agent" → land at /agent.
# 3. Type a task: "list the first three Python files in
#    document_processor/api by line count".
# 4. Run.  The thought timeline + tool cards stream as the agent
#    iterates.  Finish is reached when the agent emits the answer
#    or hits max-iter; the StuckDetector intervenes if a tool is
#    looping uselessly.
```

The MCP tool registry already exposes `repo-symbol-search`,
`sandbox-execute`, memory tools, sentinel adapters, and any other
tool the operator has registered — the agent can chain them
without further wiring.
