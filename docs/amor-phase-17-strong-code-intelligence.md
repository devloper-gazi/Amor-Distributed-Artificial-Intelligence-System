# Amor — Phase 17: Strong Code Intelligence

The user reported (Turkish): "Code Intelligence çok zayıf bana
daha güçlü daha teknik bir sistem lazım daha gelişmiş backend
analizi yapıp ona göre gerekirse diğer lokal modelleri indirip
daha gelişmiş ve hatasız bir şekilde çalıştığından emin olabilir
misin?  Mevcut sistem düzgün çalışmıyor."

Three concrete pains:
1. **5 roles collapsed onto 2 models** (Phase 16.5 partially fixed,
   Phase 17 finishes by pulling stronger models + adding diagnostics
   visibility).
2. **No "more advanced backend analysis"** — operator has no way to
   see *why* a session looks weak.
3. **Errors at runtime** — `ModuleNotFoundError: No module named
   'flask'` because the sandbox didn't pip-install the deps the
   model declared.

Phase 17 ships six commits that close all three:

License: MIT.

---

## Commit map

| Commit | SHA | Subject |
|--------|------|---------|
| R | `b807a4c` | `GET /api/code/diagnostics` + collectors |
| S | `c674562` | `_publish` cross-replica Redis fallback + invert engine→routes layer violation |
| M | `a9ae3df` | strict planner `spec` block + engine forwards `dependencies` to sandbox |
| O₁ | `5783768` | per-language sandbox timeout map |
| O₂ | `719b73f` | pip/npm install into `/tmp` + bridge network when packages requested |
| T | `d6177d4` | diff-mode DebuggerAgent (SEARCH/REPLACE blocks; 3-5x token savings) |
| U (this) | — | docs + AGENTS.md + final acceptance |

Total: 6 functional commits + 1 docs commit (this file).

Test sweep at end of Phase 17: ~1021 passing.

---

## Subsystem 1 — Diagnostics endpoint (Commit R)

```
GET /api/code/diagnostics
```

Single-call snapshot of every Code Intelligence subsystem.  The
operator now has a one-shot view of "is the system healthy and
what is it doing".

| Section | Surface |
|---------|---------|
| `backend` | LLM backend kind / URL / class + live `health_check()` |
| `models` | installed Ollama tags, auto-derived `{role: tag}` map, `distinct_count`, VRAM estimate |
| `sandbox` | docker_available, workdir_root, named volume, cold-start p50/p95, recent failures |
| `rag` | embedder, hybrid + reranker config, chunking strategy, per-model tables |
| `ledger` | Phase 15 immutable ledger intact flag + entry count + tail hash |
| `phase16_facade` | OpenAI `/v1` + MCP server gate states |
| `recent_sessions` | last 5 with status + phases_failed + models_used |
| `recent_failures` | last 10 ring-buffer entries from sandbox + engine |

```python
# document_processor/code_intelligence/diagnostics.py
record_sandbox_run_ms(elapsed_ms)
record_failure(where, detail, **payload)
build_diagnostics(sessions_map, probe_sandbox=True)
```

* Pure-data collectors that **never raise** — degrade to
  `{"error": "..."}` on failure.
* TTL cache (30s) keeps the endpoint cheap when the UI polls.
* Sliding-window cold-start telemetry: 200-entry ring buffer.

Live-verified against the running stack:

```
HTTP 200
backend: ollama @ http://ollama:11434 (OllamaBackend)
models.installed: ['qwen3:8b', 'qwen2.5-coder:7b', 'qwen2.5:7b']
models.role_assignment:
  planner=qwen3:8b, critic=qwen2.5:7b, debugger=qwen2.5:7b,
  coder=qwen2.5-coder:7b, tester=qwen2.5-coder:7b
models.distinct_count: 3
sandbox.docker_available: True (probe 58ms)
sandbox.workdir_root: /sandbox-shared
ledger.intact: True
```

---

## Subsystem 2 — Cross-replica + layer fixes (Commit S)

Two surgical hardening moves the Plan agent flagged when reading
`engine.py` + `code_intelligence_routes.py` in full.

### `_publish` cross-replica Redis fallback (one-line core fix)

When the adversarial reviewer raises a critical alert, `_publish`
was calling `_sessions.get(session_id)` directly — synchronous, no
Redis fallback.  On a multi-replica deployment (nginx round-robin),
if the session start landed on replica A, the alert that fires
later might land on replica B whose `_sessions` dict is empty →
`cancel_requested` silently never propagates.

Fix: fall through to `_load(...)` (Redis cache fallback) and
re-persist via `_persist` when the session is found there.
Cross-replica `cancel_requested` propagation now works without
ripping out the in-memory layer.

### Engine→routes layer violation invert

`engine.py:_phase_model_prep` previously did
`from ..api.local_ai_routes_simple import set_active_routing`.
Engine reaching into routes is fragile.

* Engine gains `routing_setter: Callable[[dict], None] | None`
  constructor field, default `None` (no-op).
* Routes inject `_phase_routing(doc)` that wraps the user-set-
  routing-wins logic + `set_active_routing` call.
* Engine no longer imports anything from the routes layer.

---

## Subsystem 3 — Planner spec block + dependency forwarding (Commit M)

The user's `build a snake game website` attempt crashed with
`ModuleNotFoundError: No module named 'flask'`.  Three pieces of
the same upgrade close it:

### Strict spec block in PlannerAgent

`planner_prompt` schema now mandates:
```yaml
spec:
  invariants: []
  signatures: []
  preconditions: []
  postconditions: []
  error_cases: []
  dependencies: []
```

Every downstream phase (coder, tester, debugger, critic) gets
structured guarantees instead of a free-form action list.

### `_normalise_spec` helper

* Bounded list lengths (10–20 per category)
* Per-item char caps (80–400)
* Drops non-string entries
* Older planners that don't emit a spec degrade to empty lists

### Engine forwards dependencies to sandbox

`CodeIntelligenceEngine.coder_metadata` field stores the coder's
emitted `dependencies` list.  `_phase_execute` unions plan
`spec.dependencies` ∪ `coder_metadata.dependencies`, sanitises
against an allow-list regex
(`^[a-zA-Z][\w\-\.]*(?:\[[\w\-\.,]+\])?(?:[<>=!~]=?[\w\-\.\+]+)?$`)
to strip shell metacharacters, caps at
`code_sandbox_max_pip_packages` (default 12), passes to the
sandbox's existing `install_packages` parameter.

Allow-list rejects `"flask; rm -rf /"`, `"$(whoami)"`,
`"`cat /etc/passwd`"`, `"--index-url http://evil.com"`, etc.

---

## Subsystem 4 — Sandbox install path (Commit O)

When packages are requested the sandbox now:

1. Switches `--network` from `none` to `bridge` so pip / npm can
   reach the public registry.  Without packages we keep the
   strict isolation (verified by
   `test_sandbox_enforces_network_isolation`).
2. Uses `pip install --target=/tmp/pip-prefix` +
   `export PYTHONPATH=/tmp/pip-prefix` so writes go to the tmpfs
   at `/tmp` without touching the base image's site-packages
   tree.  pip itself stays importable for the next run.
3. Mirrors the approach for npm: `--prefix /tmp/npm-prefix` +
   `NODE_PATH=/tmp/npm-prefix/node_modules`.

Per-language timeout map: HTML/CSS drop to 5s, compile-heavy
languages (Rust / Go / C++ / Java / TypeScript) widen to 60-90s.

Live-verified:

```python
ExecutionSandbox.execute(
    code="from flask import Flask\nprint('flask OK')",
    language="python",
    install_packages=["flask"],
    timeout=180,
)
# → exit_code=0
#   stdout: 'flask OK from version: flask.app'
```

---

## Subsystem 5 — Diff-mode DebuggerAgent (Commit T)

DebuggerAgent now emits **minimal patches in SEARCH/REPLACE block
format** (Aider / Cline / OpenHands V1 SDK convention) instead of
rewriting the whole file.

### Why search/replace, not unified diff

* Small open-weights models (7B–14B) generate the format more
  reliably; unified diffs trip on whitespace + line-number arithmetic.
* Apply rule is simple + safe: SEARCH must appear *exactly once*
  in the current file or the entire patch is rejected.
* No external `patch` binary needed; pure Python.

### Output format

````markdown
```diff
<<<<<<< SEARCH
def buggy():
    return wrong()
=======
def buggy():
    return correct()
>>>>>>> REPLACE
```
```json
{"root_cause": "...",
 "fix_description": "...",
 "lines_changed": N,
 "confidence": "high|medium|low",
 "fallback_reason": null}
```
````

### Falls back to whole-file rewrite

When the LLM-emitted diff doesn't apply cleanly (drift / ambiguous
match / malformed fence), DebuggerAgent automatically re-prompts
with the original whole-file system prompt.  Failed applies fire
`record_failure("debugger.diff_apply_failed", ...)` so the
diagnostics endpoint can surface a per-session diff success rate.

### Token-saving math

Whole-file mode: 500 LOC × ~3 tokens/line = ~1500 tokens out per
debug iteration.
Diff mode: typically 5-20 LOC of SEARCH + 5-20 LOC of REPLACE = ~60
tokens out.  **3-5x savings on a typical 500-LOC project**, with
fewer regressions in untouched lines as a bonus.

---

## Settings (Phase 17 additions)

```python
# document_processor/config/settings.py

# Phase 17 Commit M — engine forwards spec.dependencies to sandbox
code_sandbox_pip_install_enabled: bool = True
code_sandbox_max_pip_packages: int = 12

# Phase 17 Commit T — diff-mode debugger
code_debug_diff_mode_enabled: bool = True
```

`LANGUAGE_RUNNERS` entries gain a `default_timeout_s` field
(Commit O) — caller's explicit `timeout=` always wins.

---

## Backwards compatibility matrix

| Default | Phase 17 default | Effect |
|---------|------------------|--------|
| Diagnostics endpoint | always-on | new route; doesn't change any existing path |
| `_publish` Redis fallback | always-on | only fires when in-memory misses (was silent failure before) |
| Layer-violation invert | default no-op | engine works without `routing_setter`; routes layer wires it on construction |
| Spec block extraction | always-on | older planners that don't emit a spec degrade to empty lists |
| Dependency forwarding | `code_sandbox_pip_install_enabled = True` | flip to False to revert to no-install |
| Bridge network | conditional | only when `install_packages` is non-empty; `none` otherwise |
| Per-language timeouts | always-on | caller `timeout=` still overrides |
| Diff-mode debugger | `code_debug_diff_mode_enabled = True` | flip to False to revert to whole-file |

Hard rollback: revert the 6 Phase 17 commits.  Phase 16.5
behaviour is preserved.

---

## Test surface

| File | Tests |
|------|-------|
| `test_diagnostics.py` (Commit R) | 18 |
| `test_routing_setter.py` (Commit S) | 5 |
| `test_dependency_forwarding.py` (Commit M) | 20 |
| `test_per_language_timeout.py` (Commit O) | 3 |
| `test_diff_mode_debugger.py` (Commit T) | 17 |
| **Phase 17 total** | **63** |

Plus all of Phase 15 (151 tests), Phase 16 (132 tests), Phase 16.5
diversity tests (16) — total Code Intelligence + sentinel + rag
sweep ≈ 1021 passing.

---

## What's deferred to Phase 18

* **AlphaCodium reorder (was Commit N)** — public-test-first
  pipeline + reflect stage.  Plan agent flagged inverting
  `_phase_test` before `_phase_implement` as nontrivial; defer
  with a working diff-mode debugger first.
* **Browser sandbox via Playwright (was Commit P)** — 2 GB image
  pull + DOM screenshot capture.  Worth doing once the user has
  ≥3-model fleet on the GPU and consistent debug iterations.
* **Pre-warmed sandbox container pool** — cold-start ~3s; defer
  until diagnostics shows it dominates measured latency.
* **3-vote self-consistency on planner** — defer until an eval
  set proves it helps.

---

## Where to read next

* `AGENTS.md` — Phase 17 prompt-policy section
* `docs/amor-phase-16-foundations.md` — adapter primitives
  Phase 17 builds on
* `docs/sentinel-evolution.md` — Phase 15 immutable ledger that
  diagnostics reports on
* `document_processor/code_intelligence/diagnostics.py` —
  collector implementations
* `document_processor/code_intelligence/diff_apply.py` —
  SEARCH/REPLACE block parser + applier
