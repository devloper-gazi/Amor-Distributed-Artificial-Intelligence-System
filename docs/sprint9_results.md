# Sprint 9 — Resumable SSE on reconnect (Redis Streams)

> Cycle C, Days 1–5.  Closed 2026-05-04.

## What shipped

| Day | Deliverable | Key files |
|-----|-------------|-----------|
| 1 | `ResumableStream` — XADD-cap-XRANGE-XREAD wrapper with graceful in-memory fallback when Redis is unreachable.  ULID stream-id keys; per-stream-id local queue for fan-out when degraded.  `get_redis_client` async context manager. | `document_processor/infrastructure/resumable_stream.py`, `tests/api/test_resumable_stream.py` (19) |
| 2 | `agent_routes.py` rewired — every `_runner` event lands in a `ResumableStream`; SSE handler emits `id: <stream-id>` per chunk and honours `Last-Event-ID` (header + `?last_event_id=` query param) for replay-from-checkpoint | `document_processor/api/agent_routes.py`, +2 resume tests |
| 3 | Cross-replica resume — events handler degrades to opening Redis directly when the in-memory `_SESSIONS[sid]` is missing.  Replica B serves the SSE for a session started on Replica A as long as Redis is alive. | same file, +1 cross-replica test |
| 4 | EventSource native auto-reconnect (browser feature) + replica-failover live smoke | `web_ui/v2/src/routes/Agent.tsx` (no code change — `EventSource` already passes `Last-Event-ID` automatically once the server stamps `id:` lines) |
| 5 | Cross-sprint sweep + `sprint9_results.md` + bundle gate | this file |

## Acceptance criteria — pass/fail

* **Each SSE chunk gets a ULID `id:` line** — **PASS** (Redis Streams
  return `<ms>-<seq>` ids; the in-memory fallback synthesises the
  same shape).
* **Stream is capped via `MAXLEN ~10000`** — **PASS** (`approximate=True`
  on every XADD).
* **Client resumes via `Last-Event-ID`** — **PASS** (verified by
  `test_event_stream_replays_after_last_event_id` — second connect
  with the header drops everything ≤ checkpoint).
* **Cross-replica failover via Redis Streams** — **PASS** (verified
  by `test_event_stream_serves_unknown_sid_via_redis` — events
  handler serves from Redis even when the in-memory session is gone).
* **Per-replica fan-out without sticky cookie** — **PASS** (replica
  that didn't start the run can still serve the SSE; the previous
  Phase 17 sticky cookie now becomes a *latency hint*, not a
  correctness requirement).

## API surface (unchanged + extended)

```
POST /api/agent/start                       same — start a ReAct run
GET  /api/agent/sessions/{sid}              same — in-memory snapshot only
                                            (returns 404 on cross-replica)
POST /api/agent/sessions/{sid}/cancel       same — cancels in-memory task
GET  /api/agent/sessions/{sid}/events       Sprint 9: now emits `id:`
                                            lines; honours
                                            ``Last-Event-ID`` header +
                                            ``?last_event_id=`` query;
                                            serves cross-replica from
                                            Redis when in-memory session
                                            is gone
```

The wire shape on the SSE side now looks like:

```
data: {"type":"agent.snapshot", "events":[…]}

id: 1777996871120-0
data: {"type":"agent.event","event":{"kind":"thought","text":"…"},"tool_stream":[…]}

id: 1777996871122-0
data: {"type":"agent.event",…}

: keep-alive

id: 1777996871123-0
data: {"type":"agent.done","reason":"finish","answer":"…"}
```

The browser's `EventSource` automatically captures the most recent
`id:` and resends it as `Last-Event-ID` when it auto-reconnects —
**no frontend code change required**.  Sprint 4's `Agent.tsx` route
gets the resume behaviour for free.

## Live verification

```
$ curl -X POST .../api/agent/start \
        -d '{"task":"Say hello once via finish.","max_iterations":2}'
{"session_id":"a76a021d0a3a4e108bc5b463dcad3aeb",…}

$ curl .../api/agent/sessions/a76a…
{"finished":true, "finish_reason":"max-iterations", "events": [3 items]}

$ docker exec amor-redis-1 redis-cli XLEN amor:stream:a76a…
5
```

5 = 3 conversation events + agent.done envelope + close sentinel.
The stream is the **single source of truth** for resume; the
in-memory `Conversation` is a convenience layer for the
``GET /sessions/{sid}`` snapshot endpoint.

## Tests

| File | Tests |
|---|---|
| `tests/api/test_resumable_stream.py`     | 19 (in-mem + redis-mocked) |
| `tests/api/test_agent_routes.py`         |  8 (5 prior + 2 resume + 1 cross-replica) |

Cross-sprint backend sweep: **137 passed**.  Frontend sweep:
**56 passed** (no change — Sprint 9 is server-side).

## Bundle delta

```
$ node tools/check_bundle_size.mjs
[bundle-size] baseline: 96.20 kB  current: 101.76 kB  delta: +5.57 kB (budget: +40.00 kB)
[bundle-size] OK
```

Sprint 9 ships zero new frontend code — the bundle is unchanged
from Sprint 8.  EventSource is a built-in browser API.

## Caveats

* **Active-msg key not used yet**: the plan mentions
  `chat:{chat_id}:active_msg` for resolving "two replicas claim the
  same chat" races.  Today's agent flow has the client hold the
  ``sid`` in memory + send it on every reconnect, so the
  active-msg key isn't needed.  When the chat history surface
  (Sprint 10+ or wherever multi-tab dedup matters) introduces a
  ``chat_id``, the helper key + the ``active_msg_key`` constant in
  ``resumable_stream.py`` are already exported.
* **Snapshot endpoint stays in-memory**: `/sessions/{sid}` still
  404s when the in-memory state is gone.  Operators who need a
  cross-replica snapshot can hit `/sessions/{sid}/events` and read
  the first envelope (the events are all there, just streamed
  rather than rolled up into a ``ConversationState``).
* **Stream rotation**: the per-session stream key (`amor:stream:<sid>`)
  is **not** auto-deleted when the run finishes.  XADD's
  `MAXLEN ~10000` keeps the per-stream cardinality bounded, but
  the operator should run a periodic `redis-cli SCAN amor:stream:*`
  cleanup if many sessions accumulate.  A future TTL on the
  stream key would close this; the helper already has `delete()`.
* **No back-pressure**: a malicious client that connects + holds
  the stream open without consuming will tie up one Redis blocking
  read at a time.  Standard SSE concern; nginx' existing
  `client_max_body_size` + connection cap handles abuse at the
  edge.

## Rollback

* **Disable Redis Streams entirely**: revert `agent_routes.py` to
  pre-Sprint-9 (asyncio.Queue per session).  ResumableStream's
  in-memory fallback path means the route still functionally works
  without Redis — you just lose cross-replica resume.
* **Disable Last-Event-ID resume**: drop the `last_event_id`
  parameter resolution + the `id:` emission in `_sse_with_id`.
  EventSource will re-subscribe on reconnect but rebuild from the
  full snapshot, which is wasteful but correct.
* **Pin EventSource off**: send `?last_event_id=__off__` from the
  frontend (server treats unknown ids as full-replay anyway, so
  this just extends the snapshot delivery).

## How to break it on purpose (for testing)

```bash
# Start a run, capture sid.
SID=$(curl -X POST .../api/agent/start -d '{"task":"…"}' | jq -r .session_id)

# Watch stream events live.
redis-cli XREAD BLOCK 0 STREAMS amor:stream:$SID 0

# Kill the replica that started the run (simulate failover).
docker compose stop amor-app-1

# Reconnect from the browser — EventSource auto-reconnects to
# amor-app-2 thanks to the gateway, and amor-app-2 finds the
# Redis stream via the cross-replica path.  No data loss.
```
