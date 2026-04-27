# Mandatory Patterns — Code Intelligence Mode

Distilled from the existing AMOR codebase. Every new file in this
package must follow these patterns.

## P1 — Session lifecycle

```
1. Routes.start_handler:
   session_id = str(uuid4())
   session = {session_id, user_id, status: "started", progress: 0,
              prompt, ..., started_at: _now(), cancel_requested: False,
              chat_session_id, query_record_id, idempotency_keys}
   _sessions[session_id] = session
   await _persist(session_id, session)        # Redis durable backing
   background.add_task(_run_session, session_id)
   return {success, session_id}

2. _run_session:
   engine = Engine(..., on_event=on_event)
   try:
       result = await asyncio.wait_for(engine.run(), timeout=EFFORT_TIMEOUT[effort])
       session["status"] = "completed"
       await _persist(session_id, session)
       await _publish(session_id, {type: "done", session_id})
       persist_user_message(...)              # idempotent
       persist_assistant_message(...)         # idempotent
       mark_query_completed(...)
   except asyncio.TimeoutError: status="failed"; mark_query_failed
   except asyncio.CancelledError: status="cancelled"; mark_query_cancelled; raise
   except Exception: status="failed"; mark_query_failed
```

## P2 — SSE emission

```python
async def _publish(session_id, event):
    if "event_id" not in event:
        event = {**event, "event_id": uuid4().hex}
    queue = _event_queue(session_id)
    try:
        queue.put_nowait(event)
    except asyncio.QueueFull:
        # Sliding window: drop oldest, push newest.
        try: queue.get_nowait(); queue.put_nowait(event)
        except (asyncio.QueueEmpty, asyncio.QueueFull): pass
    await cache_manager.publish_event(channel.format(sid=session_id), event)
```

## P3 — Phase wrapping in engine

```python
async def _run_phase(self, name, runner):
    phase = self._phase_index[name]
    phase.status = "in_progress"; phase.started_at = _now()
    await self._emit({"type": "phase_start", "phase": name, "label": phase.label})
    try:
        result = await runner()
        phase.status = "completed"; phase.completed_at = _now(); phase.detail = result or {}
        await self._emit({"type": "phase_complete", "phase": name, "detail": phase.detail})
        return result
    except Exception as exc:
        phase.status = "failed"; phase.completed_at = _now(); phase.detail = {"error": str(exc)}
        await self._emit({"type": "phase_failed", "phase": name, "error": str(exc)})
        return None
```

## P4 — Frontend view lifecycle

```javascript
class XView {
    constructor(prompt, effort, ...) { this.root = this._renderShell(); }
    getElement() { return this.root; }
    showTimeline(snapshot) {}
    handleEvent(evt) { switch (evt.type) { ... } }
    loadFromSnapshot(snap) { this._applySnapshot(snap); }
    toSnapshot() { return {...}; }
    static fromSnapshot(snap) { const v = new XView(...); v.loadFromSnapshot(snap); return v; }
}
```

## P5 — Persistence linkage

Every AI handler accepts these four optional fields and forwards them
through the engine into the persistence helpers:
```
chat_session_id, query_record_id,
user_message_idempotency_key, assistant_message_idempotency_key
```

Backend writes share the same idempotency key as the frontend's
`_persistChatMessage()` so the unique sparse index on
`chat_messages.idempotency_key` collapses to one row.

## P6 — Ownership check

```python
def _require_owner(session: dict, user: User):
    owner = session.get("user_id")
    if owner and str(owner) != str(user.id):
        raise HTTPException(status_code=404, detail="Session not found")
```
404, not 403 — never leak existence.

## P7 — JSON extraction (LLM output)

`_extract_json(raw)` with three fallbacks:
1. `json.loads(raw.strip())`
2. Fenced ```json ... ``` block
3. Widest balanced `{...}` span + trailing-comma cleanup

Else `raise ValueError`.

## P8 — Async cancellation propagation

Engines must check `session.get("cancel_requested")` between phases or
use `asyncio.CancelledError` as the natural propagation. Either way,
the `_run_session` cancel branch ALWAYS:
1. Sets `status="cancelled"`, `error="Cancelled by user."`, `completed_at`.
2. Persists.
3. Publishes `{type: "cancelled", session_id}`.
4. Calls `mark_query_cancelled(query_record_id)`.
5. Re-raises `CancelledError` so the asyncio task tree unwinds clean.

## P9 — Logger style

`structlog`-compatible event-based logging:
```python
logger.info("event_name", key1=value1, key2=value2)
logger.warning("event_name_failed", error=str(exc))
```
Never `logger.info("Event %s with %s", value1, value2)` for new code.

## P10 — Lifespan task hooks

Long-running background tasks attach to lifespan:
```python
async def lifespan(app):
    ...
    task = asyncio.create_task(my_loop())
    yield
    task.cancel()
    try: await task
    except (asyncio.CancelledError, Exception): pass
```
The sweeper at `_sse_queue_sweeper` is the canonical example.

## P11 — Forbidden imports

In `document_processor/code_intelligence/**/*.py` and
`web_ui/static/js/code-view.js`:
- ❌ `import anthropic`
- ❌ `from .chat_research_routes import anthropic_client`
- ❌ `httpx.get("https://api.openai.com/...")`
- ❌ `httpx.get("https://api.anthropic.com/...")`
- ❌ `httpx.get("https://api.cohere.com/...")`
- ❌ `httpx.get("https://api.voyageai.com/...")`

The only allowed LLM-bridge import is:
```python
from ..api.local_ai_routes_simple import call_ollama
```
done lazily inside route handlers (never at module top, to avoid
circular module load).
