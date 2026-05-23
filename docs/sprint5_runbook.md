# Sprint 5 v18 — Approval flow runbook

> Cycle F Sprint 5 — `ApprovalPolicy.decide()` gate wraps every
> MCP tool dispatch (Wrong #3 fix).  Three-list policy
> (allow_silent / deny / prompt) + category-based fallthrough +
> cost circuit-breaker.  OFF by default.
>
> **2026-05-15 update**: SSE bridge + `/api/approval/{request_id}`
> endpoint + MCP-route engine integration landed.  PROMPT
> decisions now flow end-to-end: ToolRegistry.dispatch → policy
> PROMPT → request_user_approval (publish `approval_required`
> SSE event) → browser POST /api/approval/{id} → future resolved
> → tool runs (or rejected).

## What landed (Sprint 5 prep — gate + policy class)

| Artifact | Path | Purpose |
|---|---|---|
| Policy class | `local_ai/approval/policy.py` (NEW) | `ApprovalPolicy.decide()` returns `ApprovalDecision` (ALLOW / PROMPT / DENY / BUDGET_EXCEEDED).  Resolution order: deny-name → allow-name → prompt-name → category → default. |
| Cost circuit-breaker | `local_ai/approval/policy.py` | Per-session token budget (`CostCircuitBreaker.charge()`), trips idempotently when exceeded. |
| Settings -> policy | `local_ai/approval/policy.py:settings_to_policy()` | Tolerant CSV + JSON parser.  Malformed inputs fall through to safe defaults. |
| Default policy + lazy refresh | `local_ai/approval/policy.py:DEFAULT_POLICY` + `refresh_default_policy()` | Module-level singleton, rebuilt in-place from settings when any input changes.  Cheap tuple-key cache. |
| Tool.category attr | `local_ai/tools/base.py:Tool.category="unclassified"` | Subclasses override to one of `ApprovalCategory` values (read/write/delete/exec/network/git/db/docker/llm/secret/package). |
| Dispatch gate | `local_ai/tools/registry.py:dispatch()` | Before validate-and-execute, runs through `DEFAULT_POLICY.decide()`.  DENY → MCPToolResult(error="denied"); PROMPT + no callback → fail closed; PROMPT + callback → await user; ALLOW → continue. |
| Settings | `config/settings.py` | 7 new `code_approval_*` settings (master gate, lists, category_actions JSON, default_action, cost budget). |
| Tests | `tests/local_ai/test_approval_policy.py` (25), `test_dispatch_gate.py` (7) | **32 new tests, 252/252 gate green.** |

## Architectural decisions

* **OFF by default + lazy settings refresh.**  `DEFAULT_POLICY`
  starts disabled.  Dispatch calls `refresh_default_policy()` on
  every invocation; the function does a cheap tuple-key check and
  rebuilds the policy only when settings actually changed.  No
  startup hook required.
* **In-place singleton mutation.**  `refresh_default_policy()`
  updates the existing `DEFAULT_POLICY` object's fields rather
  than rebinding the module attribute.  This means
  `from local_ai.approval import DEFAULT_POLICY` users keep their
  reference live across settings reloads.
* **Fail-closed on PROMPT without UI.**  If a tool is in the
  `prompt` bucket but no `approval_callback` was passed to
  `dispatch()`, the call is rejected (not allowed).  Safer than
  the alternative; the SSE UI bridge (next sprint commit)
  supplies the callback.
* **Tool.category as the policy join key.**  Existing tool
  subclasses (sentinel adapter, consortium adapter) inherit
  `category="unclassified"` — under the default category mapping
  they route to `prompt`.  When the policy is OFF (default),
  this has no effect.  When ON, operators choose: tag every tool
  with a category, OR list every tool by name, OR rely on the
  `unclassified → prompt` fallthrough.
* **Cost circuit-breaker is orthogonal.**  Sprint 5's policy
  returns one of 4 decisions; the breaker is a separate signal
  the call site combines (e.g.: "policy says ALLOW but breaker
  says BUDGET_EXCEEDED → block").  Engine integration comes in
  the SSE-bridge commit.

## How to turn it on (after the SSE bridge lands)

```bash
# 1. Set env vars (in .env or compose):
echo 'AMOR_CODE_APPROVAL_ENABLED=true' >> .env
echo 'AMOR_CODE_APPROVAL_ALLOW_SILENT=read_file,search_codebase,list_dir,compile_check' >> .env
echo 'AMOR_CODE_APPROVAL_DENY=shell.rm_rf,git.force_push,db.drop_table' >> .env
echo 'AMOR_CODE_APPROVAL_DEFAULT_ACTION=prompt' >> .env
echo 'AMOR_CODE_APPROVAL_CATEGORY_ACTIONS={"delete":"deny","db":"deny","secret":"deny"}' >> .env
echo 'AMOR_CODE_APPROVAL_COST_BUDGET_TOKENS=50000' >> .env

docker compose restart app

# 2. Verify the policy is enabled:
python -c "
from local_ai.approval import DEFAULT_POLICY, refresh_default_policy
refresh_default_policy()
print('enabled =', DEFAULT_POLICY.enabled)
print('deny    =', DEFAULT_POLICY.deny_tools)
"

# 3. The first build session that tries to invoke a `delete`-
#    category tool will block at dispatch with `approval_denied`
#    metadata in the MCPToolResult.
```

## SSE bridge + HTTP endpoint (2026-05-15 update)

### Architecture

```
┌─────────────┐                  ┌──────────────────┐
│  agent /    │  registry        │  ToolRegistry    │
│  engine     │  .dispatch()  →  │  approval gate   │
└─────────────┘                  └────────┬─────────┘
                                          │ PROMPT decision
                                          ▼
                              ┌────────────────────────┐
                              │ request_user_approval()│
                              │  + future              │
                              └───┬────────────────┬───┘
                  publish SSE on  │                │ await future
                  session channel ▼                │
                          ┌──────────────┐         │
                          │   browser    │         │
                          │   shows      │         │
                          │   approval   │         │
                          │   prompt     │         │
                          └──────┬───────┘         │
                                 │ POST /api/approval/{id}
                                 ▼                │
                          ┌──────────────────┐    │
                          │ approval route   │    │
                          │ resolve_approval │────┘ (locally)
                          │ + redis publish  │     OR
                          └──────────────────┘     (cross-replica via
                                                   handle_cross_replica_decision)
```

### Files added (2026-05-15 commit)

| Path | Purpose |
|---|---|
| `document_processor/api/approval/__init__.py` | Package surface (`request_user_approval`, `resolve_approval`, `approval_router`, `register_approval_routes`) |
| `document_processor/api/approval/bridge.py` (~180 LOC) | `AwaitingApproval` dataclass, in-process future registry, Redis persist + cross-replica wakeup helpers |
| `document_processor/api/approval/routes.py` (~80 LOC) | `POST /api/approval/{request_id}` + `GET /api/approval/_pending` debug endpoint |
| `document_processor/main.py` | `APPROVAL_ROUTES_AVAILABLE` import + `app.include_router(approval_router)` |
| `document_processor/api/mcp_routes.py` | `ToolCallRequest` gains optional `session_id` + `actor_role` fields; `call_tool` builds an `approval_callback` closure that bridges to `request_user_approval` when `session_id` is supplied |
| `tests/api/test_approval_bridge.py` (6) + `test_approval_routes.py` (7) | Coverage for future resolution, timeout, registry hygiene, HTTP shape, debug endpoint, cross-replica fallthrough |

### Wire shape

```
POST /api/approval/{request_id}
    Content-Type: application/json
    Body: {"approved": <bool>, "note": "<optional reason>"}

Responses:
    200 {"resolved": true,  "approved": <bool>}                  # local future resolved
    200 {"resolved": false, "approved": <bool>, "via": "redis"}  # broadcast (cross-replica)
    400 {"detail": "invalid request_id"}
    422 {"detail": "missing required field 'approved'"}
```

### SSE event emitted on PROMPT

```json
{
  "type": "approval_required",
  "request_id": "<32-char hex>",
  "tool_name": "rm_rf",
  "category": "delete",
  "arguments": {"path": "/tmp/work"},
  "actor_role": "coder",
  "timeout_s": 90.0,
  "event_id": "<32-char hex>"
}
```

On resolution (or timeout):

```json
{
  "type": "approval_resolved",
  "request_id": "<32-char hex>",
  "approved": <bool>,
  "reason": "timeout" | null
}
```

## Sprint 5 exit criteria

| # | criterion | status |
|---|---|---|
| 1 | Every MCP tool call passes through `ApprovalPolicy.decide()` | **landed** (registry.dispatch wrap) |
| 2 | 20-prompt red-team test of destructive ops: 100% gated | needs red-team script (deferred) |
| 3 | runc CVE-2025-31133 reproduction fails to escape | docs-only: requires Docker Desktop ≥4.30 (bundled runc ≥1.2.x) |
| 4 | Cost circuit-breaker trips when synthetic prompt exceeds budget | unit test green ✓; engine integration is SSE-bridge work |
| 5 | CI test sweep delta: +25 tests, all green | **landed +45 tests (265/265)** (32 policy + 13 SSE-bridge) |
| 6 | Rollback verified: `AMOR_CODE_APPROVAL_ENABLED=false` reverts | **verified by `test_disabled_policy_dispatch_runs_tool`** |
| 7 | Engine ↔ approval bridge live-verified | **2026-05-15 ✓** — `/api/approval/_pending` mounted, returns `{count: 0}` on fresh stack |

Remaining Sprint 5 work (small):
* **20-prompt red-team script** — `tests/red_team/destructive_ops.sh`.  Drives 20 synthetic prompts that should trigger destructive tool calls, asserts 100% are gated.
* **Wrong #2 default flip** — change `AMOR_DOCKER_HOST` default to `tcp://amor-docker-proxy:2375` in `docker-compose.yml`.  Risk: needs full sandbox-run smoke against the proxy first to confirm allow-list is sufficient.
* **`ApprovalPrompt.tsx`** — browser inline approval card.  Subscribes to `approval_required`, displays category + arguments, POSTs to `/api/approval/{id}`.

## Rollback

| change | rollback |
|---|---|
| Approval gate | `AMOR_CODE_APPROVAL_ENABLED=false` + restart app |
| Specific deny | Remove the tool name from `AMOR_CODE_APPROVAL_DENY` and refresh |
| Stuck PROMPT decisions | Add the tool to `AMOR_CODE_APPROVAL_ALLOW_SILENT` |
| Cost budget tripped | Restart app (each new session gets a fresh CircuitBreaker) |
| Dispatch gate code | Revert the `try: from local_ai.approval import ...` block in `local_ai/tools/registry.py:dispatch()` — restores Sprint 4 behaviour |

No DB migration, no schema change.

## Caveats

* **The `approval_callback` is not yet wired.**  Sprint 5 lands
  the dispatch signature `dispatch(..., approval_callback=None)`;
  the SSE bridge that supplies the callback (sends
  `approval_required` events, awaits `POST /api/approval/{id}`)
  is the next Sprint 5 commit.  Until then, PROMPT decisions
  fail-closed.
* **runc pin is documented but not yet committed.**  The
  `Dockerfile.sandbox` pin (≥1.2.8 to mitigate the November
  2025 CVE chain) is on the punch list.  Until then, AMOR
  inherits whatever runc the Docker Desktop / Engine version
  ships.
* **Existing tools inherit `category="unclassified"`.**  Under
  the default category mapping, unclassified routes to `prompt`.
  When the policy gate is enabled, operators MUST either tag
  their tools, list them explicitly, or set
  `code_approval_default_action="allow"` (defeats the gate's
  purpose, but useful for migration).
* **CostCircuitBreaker is process-local.**  Cross-replica budget
  tracking (forward-compat for re-enabling 2 replicas later)
  requires Redis state — deferred to v19.
* **Pre-existing test failures in `tests/code_intelligence/
  test_dependency_forwarding.py` + `test_html_routing.py` are
  unrelated** (Cycle D dependency-filter test drift, called out
  in Sprint 2 runbook).

## Wire-shape reference

```python
from local_ai.approval import ApprovalRequest, DEFAULT_POLICY

# 1. Build request from runtime context
req = ApprovalRequest(
    tool_name="rm_rf",
    category="delete",
    arguments={"path": "/tmp/work"},
    session_id="<session-uuid>",
    actor_role="coder",
)

# 2. Decide
decision = DEFAULT_POLICY.decide(req)
# decision ∈ {ALLOW, PROMPT, DENY, BUDGET_EXCEEDED}

# 3. Dispatch handles routing automatically; just pass the
#    callback for PROMPT paths:
result = await registry.dispatch(
    "rm_rf", {"path": "/tmp/work"},
    session_id="<session-uuid>",
    actor_role="coder",
    approval_callback=ask_user_via_sse,  # awaitable returning bool
)
```
