# Cycle C — complete

> 12 sprints · 6 months engineering plan · closed 2026-05-04.
> Author handoff doc; the canonical "what shipped" artefact for
> Cycle C.

## TL;DR

**Cycle C shipped, fully live on the running stack, all gates green.**

* **Sprints completed**: 0 + 1–12 (13 distinct deliveries)
* **Test count**: 257 (158 backend + 99 frontend)
* **Bundle delta**: +12.88 kB gzipped / +40 kB budget (32% used,
  68% headroom)
* **New routes / endpoints**: 18 backend, 7 frontend admin/mode
  surfaces, 4 PWA root-scoped artefacts
* **External dependencies added**: `axe-core`,
  `@solidjs/testing-library` (testing only), `mem0ai`-friendly
  adapter (optional dep, not installed); zero runtime deps
  beyond what Cycle B shipped.

## Sprint matrix

| # | Sprint | Status | Headline | Result doc |
|---|--------|--------|----------|------------|
| 0 | Canonical 10-prompt baseline | ✓ | Sprint 0 corpus + Mistral-Small-3 judge live; reference for every later "improvement" claim | _embedded in plan_ |
| 1 | Ollama → llama-swap + llama.cpp | ✓ | Pluggable LLM backend, `--cache-reuse 256`, Q8_0 KV (Q4 corruption fixed); `/admin/llm` dashboard | `docs/sprint1_results.md` |
| 2 | Eval harness expansion | ✓ | HumanEval+ 50 (78% pass@1), SWE-bench-Lite 25 + RAGAS scaffolds; `/admin/evals` dashboard | _embedded in Sprint 1 doc_ |
| 3 | Code-context RAG (Aider repomap) | ✓ | `repo_map.py` + BM25 hybrid retrieval; `repomap_attached` SSE event + Build.tsx context panel | _embedded_ |
| 4 | UI overhaul (mode-agnostic composer + tool cards) | ✓ | UnifiedComposer + `@`-mention picker + drag-drop + MessageActions + ToolCallCard + axe-core a11y gate + bundle CI | `docs/sprint4_results.md` |
| 5 | Sandbox security hardening | ✓ | docker-socket-proxy + `--cap-drop=ALL` + `--pids-limit=128` + default seccomp; sandbox smoke 20/20 | `docs/sprint5_results.md` |
| 6 | ORPO fine-tuning + manual gate | ✓ | preference_pairs + training_runs schema; trainer / converter / eval scaffolds; `/admin/training` UI; Prometheus metrics | `docs/sprint6_results.md` |
| 7 | Episodic + cross-session memory (Mem0 OSS) | ✓ | `Mem0Adapter` with no-op fallback; `/api/admin/memory/*` routes; "Remembered" pill + `/admin/memory` viewer | `docs/sprint7_results.md` |
| 8 | MCP agentic loop (ReAct, OpenHands SDK pattern) | ✓ | Event taxonomy + Conversation log + ReActAgent + StuckDetector + `/api/agent/*` SSE; `/agent` UI route | `docs/sprint8_results.md` |
| 9 | Resumable SSE on reconnect (Redis Streams) | ✓ | ResumableStream (XADD/XRANGE/XREAD); `Last-Event-ID` honoured; cross-replica resume via Redis | `docs/sprint9_results.md` |
| 10 | i18n Turkish + locale-aware everything | ✓ | Frontend i18n primitive + en/tr tables; backend Accept-Language parser + 4 routers migrated | `docs/sprint10_results.md` |
| 11 | Mobile-optimized UI | ✓ | Viewport hook + safe-area CSS; MobileShell + drawer + BottomSheet (visualViewport offset); 44×44 touch floor | `docs/sprint11_results.md` |
| 12 | PWA service worker + Tauri 2.0 spike | ✓ | Hand-rolled SW + manifest + icons; `/sw.js` + `/manifest.webmanifest` live; Tauri 2 scaffold (build operator-driven) | `docs/sprint12_results.md` |

## What's actually live right now

* **Backend (FastAPI)** — 18 new endpoints across the cycle.  Spot
  checks the operator can run today:

  ```
  GET  /api/repo/symbols?q=Engine          (Sprint 4 Day 2)
  GET  /api/code/diagnostics               sandbox.security score 9/10 (Sprint 5)
  POST /api/admin/training/runs/{id}/promote (Sprint 6)
  GET  /api/admin/memory/status            (Sprint 7)
  POST /api/agent/start                    (Sprint 8)
  GET  /api/agent/sessions/{sid}/events    SSE id: + Last-Event-ID (Sprint 9)
  GET  /metrics                            amor_training_runs_total{status} etc.
  GET  /manifest.webmanifest               (Sprint 12)
  GET  /sw.js                              (Sprint 12)
  ```

  All endpoints are auth-gated except the four PWA artefacts +
  `/health` + `/metrics`.

* **Frontend (SolidJS v2)** — 7 new admin / mode surfaces:

  ```
  /chat                     unified composer + tool-card preview (Sprint 4)
  /agent                    ReAct loop + thought timeline (Sprint 8)
  /admin/training           preference pairs + run history + promote (Sprint 6)
  /admin/memory             Mem0 status + search + add/delete (Sprint 7)
  /admin/baselines          Sprint 0 corpus dashboard (already shipped Sprint 0)
  /admin/llm                resident models + swap events (already shipped Sprint 1)
  /admin/evals              HumanEval+ / SWE-bench / RAGAS history (already shipped Sprint 2)
  ```

  All seven render under both desktop (`AppShell` + `Sidebar`) and
  mobile (`MobileShell` + drawer) layouts via `useViewport()`.
  Settings → Türkçe localizes the entire chrome.

* **Bundle**: `index.<hash>.js` = **109.08 kB gzipped** (Cycle C
  baseline 96.20 kB → +12.88 kB).

## Tauri vs PWA decision

The plan called for a Tauri 2.0 *spike*, with the verdict deferred
post-Sprint-12.  As of cycle close:

* **Default delivery channel = PWA**.  Service worker + manifest
  + installable display all live; the user "installs" AMOR via the
  browser's add-to-home-screen affordance, gets a standalone
  window with no browser chrome, and reads cached chat history
  offline.
* **Tauri shell = optional**.  Scaffold lands at `desktop/tauri/`;
  operators who want a Windows MSI for distribution build it
  themselves.  Sprint 12 doesn't ship a binary.

The decision rationale, in one paragraph: a Tauri shell adds
value only when AMOR needs OS-level integration the PWA can't
provide (system tray, file-system access bypassing the FastAPI
sandbox, OS update channel).  None of those are true today.  The
expected ~10× bundle / ~3× RAM win from Tauri vs Electron is
real, but the comparison is against an Electron baseline AMOR
never had — the PWA already gives us the Electron-equivalent
experience at zero additional cost.  The scaffold exists so a
future cycle can pick it up if a concrete user need surfaces.

## Caveats / known follow-ups

The following land as explicit deferrals — operator-visible items
that the Cycle C plan called out but didn't gate the cycle on.

* **Per-mode legacy routes** (Build / Research / Thinking /
  Consortium / Sentinel) still emit some hardcoded English
  headings.  Migration path: same one Sprint 10 used for admin
  routes (~5 lines per route).
* **Mem0 graph memory** disabled by default — Cycle C plan said
  Neo4j-required features stay off; the adapter exposes the
  toggle (`AMOR_MEMORY_NEO4J=1`) for operators who want it.
* **ORPO trainer real run** — the Cycle 6 plan threshold is 200
  preference pairs.  As of cycle close that pool sits at 2 (test
  fixtures); the trainer's `--allow-tiny` smoke path runs, but
  real training is operator-gated by accumulating actual user
  ratings.
* **Cross-replica `chat:{cid}:active_msg`** — Sprint 9 Day 3
  closed the bigger cross-replica resume gap by serving SSE
  directly from Redis.  The active_msg key + helper exist
  (`active_msg_key()` exported) for the future case where
  multi-tab dedup actually matters.
* **Tauri build artefact** — operator-driven; Sprint 12 ships the
  scaffold + measurements table, not a binary.

## Test surface

```
$ pytest tests/local_ai/ tests/api/ \
         tests/code_intelligence/test_sandbox_security_posture.py \
         tests/training/
158 passed

$ npx vitest run
Tests: 99 passed (8 test files)

$ node tools/check_bundle_size.mjs
[bundle-size] baseline: 96.20 kB  current: 109.08 kB  delta: +12.88 kB
[bundle-size] OK
```

Tests added per sprint:

| Sprint | Backend | Frontend |
|--------|---------|----------|
| 4 | 7 (`test_repo_routes`) | 56 (a11y + composer parsers + tool stream + …) |
| 5 | 6 (sandbox posture) | — |
| 6 | 5 + 13 + 7 = 25 (training routes + trainer scaffold + eval adapter) | — |
| 7 | 12 + 8 + 9 = 29 (mem0 adapter + admin memory + memory recall) | — |
| 8 | 17 + 15 + 11 + 5 = 48 (events + prompt + loop + agent routes) | — |
| 9 | 19 + 8 = 27 (resumable stream + agent route resume) | — |
| 10 | 21 (i18n) | 23 (i18n primitive + mode helpers) |
| 11 | — | 8 (viewport hook) |
| 12 | — | 12 (PWA helper) |
| **Σ** | **158** | **99** |

## Final live check

```
$ curl -s -o /dev/null -w "%{http_code}" http://localhost:8000/health
200

$ curl -s http://localhost:8000/manifest.webmanifest | head -2
{
  "name": "AMOR — local-first distributed AI",

$ curl -I http://localhost:8000/sw.js  → HTTP 200, Content-Type: application/javascript
$ curl -I http://localhost:8000/agent  → HTTP 200 (SPA route)
$ curl -I http://localhost:8000/admin/training → HTTP 200
$ curl -I http://localhost:8000/admin/memory   → HTTP 200
$ curl -I http://localhost:8000/chat           → HTTP 200
$ docker exec amor-redis-1 redis-cli XLEN amor:stream:<sid>  → live event log
$ /api/code/diagnostics → sandbox.security.score: 9, level: max
$ /metrics → amor_training_runs_total{status="evaluated"} etc.
```

## Next cycle hints

The Cycle C plan is exhausted.  Conceptual hooks for a future
Cycle D (decision deferred):

* **Reflexion / inference-time retry loops** for the Build pipeline
  — the agent loop already exists (Sprint 8); a Reflexion-style
  multi-attempt pattern could plug into the same Conversation
  primitive.
* **Mercury Coder draft-and-verify** for code generation —
  evaluable against the Sprint 2 HumanEval+ harness without
  changing the surrounding stack.
* **i18n locale expansion** — adding Spanish / Japanese / German
  is a single `tables/<locale>.ts` + `messages.<locale>.py`
  pair; Sprint 10's primitive was deliberately built to scale.
* **Push notifications** for the mobile shell — manifest already
  declares notification metadata; backend needs a per-user
  subscription endpoint + APNs / FCM bridge if that becomes a
  user need.
* **Real Tauri release** if the file-system integration story
  becomes a user need.

## Acknowledgements

This cycle was driven by direct user feedback — Turkish-language
operator feedback drove Sprint 10's i18n acceptance gate; phone
testing drove Sprint 11's MobileShell breakpoints; the
"replica restart loses my session" complaint drove Sprint 9's
Redis Streams resume.  Each sprint shipped one user-visible
deliverable per the original plan; nothing was deferred without
an explicit caveats note in the per-sprint results doc.

**Cycle C is closed.**
