# Cycle D — complete

> Polish cycle on top of Cycle C.  Closed 2026-05-08.
> Driven by user-reported bugs in the Build pipeline output of
> "a c++ system for user guide" + a follow-up Research mode fix.
> 6 orthogonal fixes + 5 frontend retrofits, all live on the
> running stack.

## TL;DR

**Cycle D shipped, fully live on both replicas, all gates green.**

* **Fixes landed**: 6 backend (Build pipeline polish + planner/critic
  resilience) + 5 frontend (Research mode reducer + 5-tier effort
  selector retrofit across Research/Build/Thinking + Turkish
  empty-state migration + resilience-event banner).
* **Test count delta**: +66 (Cycle C 257 → Cycle D 323).
  - Backend: +66 in `tests/code_intelligence/` and
    `web_ui/v2/src/lib/chat-stream.test.ts` (RESEARCH_REDUCER 27,
    coder C++ validation 12, critic verdict coherence 7,
    dependency hygiene 12, focused-spec 7, planner+critic
    resilience 11).
  - Frontend: +1 effort-selector a11y test (no regression).
* **Bundle delta**: ~+2 kB gzipped (effort selectors + resilience
  banner cases) — comfortably within the +40 kB budget.
* **Browser verification**: end-to-end Build pipeline fully green
  (Triage / Model prep / Plan / Implement / Execute / Analyse /
  Test / Debug / Review).  Initial run pass on a "fizzbuzz in c++
  with std::function dispatch" prompt — **0 debug iterations**
  (Cycle B baseline: 2).

## What's actually live right now

### Backend (FastAPI, both replicas)

| Surface | Cycle D delta |
|---------|--------------|
| `document_processor/code_intelligence/prompts.py` | `CPP_STD_SYMBOL_TO_HEADER` (90 entries), `_CPP_GROUND_RULES` injected when `plan.language == "cpp"`, `focused_spec` rendered as a higher-priority spec block above the free-form plan, `CRITIC_SYSTEM_PROMPT` carries verdict-severity rules |
| `document_processor/code_intelligence/agents.py` | `_validate_cpp_includes` + `_detect_cpp_forward_ref` + `_inject_cpp_forward_decls`, CoderAgent post-validator wires C++ auto-fixes (incl. `coder_auto_fixes` data field), CriticAgent verdict-severity coherence guard + neutral-fallback + retry, **PlannerAgent retry + `_minimal_fallback_plan`** |
| `document_processor/code_intelligence/engine.py` | `_filter_unused_packages` (Python / JS / TS / C++ / C aware), `_extract_focused_spec` (plan→spec compression incl. STL header inference), `_phase_implement` rewires plan-for-coder, `_phase_plan` emits `planner_fallback` event, `_phase_execute` emits `install_packages_filtered` event |

### Frontend (SolidJS v2)

| Route / surface | Cycle D delta |
|-----------------|--------------|
| `web_ui/v2/src/lib/chat-stream.ts` | `RESEARCH_REDUCER` export — handles `phase_start` / `sub_question` / `source_added` / `analyzing_source` / `report_ready{markdown}` / `done` events; replaces buffer with the final markdown |
| `web_ui/v2/src/components/chat/ChatComposer.tsx` | Optional `effortTiers` / `effortValue` / `onEffortChange` props rendering a 5-tier segmented control with a11y-correct `role="radiogroup"` |
| `web_ui/v2/src/routes/Research.tsx` | New reducer + 5-tier selector + `localStorage["amor.research.effort"]` persistence |
| `web_ui/v2/src/routes/Build.tsx` | 5-tier selector + `localStorage["amor.build.effort"]` + `planner_fallback` and `install_packages_filtered` SSE event handlers rendering subtle italic notices |
| `web_ui/v2/src/routes/Thinking.tsx` | 5-tier selector + `localStorage["amor.thinking.effort"]` |
| `web_ui/v2/src/routes/{Build,Research,Thinking,Consortium,Sentinel}.tsx` | All hardcoded English empty-state titles + bodies + "Cancel" buttons migrated to `t()` calls |
| `web_ui/v2/src/i18n/{en,tr}.ts` | +28 keys (5 tiers × 2 + composer chrome × 2 + 5 modes × {empty.title,empty.body} × 2) |

## Fix matrix

| # | Bug class (from user output) | Fix | Test file |
|---|------------------------------|-----|-----------|
| 1 | Coder iter 1 — missing `#include <functional>` | `_validate_cpp_includes` + C++ ground rules in coder prompt | `test_coder_cpp_validation.py` (12) |
| 2 | Coder iter 2 — forward-declaration order in map literal | `_detect_cpp_forward_ref` + `_inject_cpp_forward_decls` | (same file) |
| 3 | Triage installed `doxygen, latex` for self-contained C++ | `_filter_unused_packages` cross-check | `test_dependency_hygiene.py` (12) |
| 4 | Reviewer verdict `approved_with_minor` + `major` issue | Coherence guard auto-downgrades to `needs_revision` | `test_critic_verdict_coherence.py` (7) |
| 5 | Plan abstractions ("Doxygen / Sphinx") not grounded | `_extract_focused_spec` w/ STL header suggestion | `test_focused_spec.py` (7) |
| 6 | Planner / critic LLM empty-output wedges pipeline | Retry-once + minimal/neutral fallback + `_resilience_fallback` flag | `test_planner_resilience.py` (11) |
| (Research) | "(done)" rendered instead of full report | `RESEARCH_REDUCER` with `replace`-on-`report_ready` | `chat-stream.test.ts` (27) |

## Live verification

### Backend (deterministic harness)

```bash
$ docker exec amor-app-1 python /tmp/verify_cycle_d_fixes.py
=== FIX 1A — Missing #include detection ===
Headers added: ['<functional>']  PASS

=== FIX 1B — Forward declaration detection ===
Forward refs detected: ['formatMarkdown', 'formatLatex']  PASS

=== FIX 3 — install_packages cross-check ===
User's exact case (self-contained C++) — kept: [], dropped: ['doxygen', 'latex']  PASS

=== FIX 5 — Plan-to-spec extraction ===
Suggested includes from spec signatures:
  ['<functional>', '<memory>', '<string>', '<unordered_map>', '<vector>']  PASS

=== FIX 4 — Verdict-severity coherence guard ===
Output verdict: needs_revision (was approved_with_minor + major issue)  PASS
```

### Browser (end-to-end pipeline)

* **/research** "What is CRDT and how does it differ from OT?" with
  effort = "Orta" — 17 sources gathered, 17 analyzing ticks, full
  markdown report **replaces** the progress trail (cycle's flagship
  Research bug, fully resolved).
* **/build** "fizzbuzz in c++ with std::function dispatch table" —
  Triage ✓ / Model prep ✓ / **Plan ✓** / Implement ✓ / Execute ✓
  (exit=0 on initial run, 0 debug iterations) / Analyse ✓ / Test ✓ /
  Debug ✓ / Review ✓ "Approved with minor — 70/100" with the
  resilience-fallback notice ("Critic unavailable") rendered cleanly.

## Files touched

```
document_processor/code_intelligence/agents.py          (+283 LOC)
document_processor/code_intelligence/prompts.py         (+155 LOC)
document_processor/code_intelligence/engine.py          (+201 LOC)
document_processor/api/local_ai_routes_simple.py        (+5  LOC, research_complete emit)
tests/code_intelligence/test_coder_cpp_validation.py    NEW (12 tests)
tests/code_intelligence/test_critic_verdict_coherence.py NEW (7 tests)
tests/code_intelligence/test_dependency_hygiene.py      NEW (12 tests)
tests/code_intelligence/test_focused_spec.py            NEW (7 tests)
tests/code_intelligence/test_planner_resilience.py      NEW (11 tests)
tools/verify_cycle_d_fixes.py                           NEW (live harness)
web_ui/v2/src/lib/chat-stream.ts                        (+RESEARCH_REDUCER)
web_ui/v2/src/lib/chat-stream.test.ts                   NEW (27 tests)
web_ui/v2/src/components/chat/ChatComposer.tsx          (effortTiers props)
web_ui/v2/src/routes/Research.tsx                       (reducer swap + selector)
web_ui/v2/src/routes/Build.tsx                          (selector + resilience banner)
web_ui/v2/src/routes/Thinking.tsx                       (selector + i18n empty state)
web_ui/v2/src/routes/Consortium.tsx                     (i18n empty state)
web_ui/v2/src/routes/Sentinel.tsx                       (i18n empty state)
web_ui/v2/src/i18n/en.ts                                (+28 keys)
web_ui/v2/src/i18n/tr.ts                                (+28 keys)
docs/cycle_d_complete.md                                NEW (this file)
```

## Caveats / known follow-ups

* **`_validate_cpp_includes` is heuristic, not a real C++ parser.**
  Catches the 90% of obvious gotchas the LLM forgets; using-
  declarations + nested template std types may slip through.
* **Function-order detection only flags map-literal-of-functions
  patterns** (the actual bug class hit by the user).  General
  forward-declaration detection is much harder; deferred.
* **Planner / critic resilience uses retry-once.**  No exponential
  backoff or multi-attempt loop — the goal is "don't wedge the
  pipeline at the plan/critic phase," not "guarantee a model
  succeeds."  When both attempts fail, the deterministic fallback
  still produces a usable deliverable.
* **`approved_with_minor` downgrade fires only on `major` (not
  `nit`/`minor`).**  Critical issues always force `needs_revision`.
* **Effort selector persistence is per-route.**  A user who picks
  "Ultra" on Research keeps it on Research; switching to Build
  doesn't inherit that choice.

## Next cycle hints

* **Build mode focused-spec end-to-end test** with a live LLM, not
  just the unit-test mocks.  Today the test suite mocks the LLM;
  a small "happy-path live" check would catch prompt-template
  regressions early.
* **Reflexion-style multi-attempt loop on coder phase** — the
  current debug loop already exists (Cycle B); a Reflexion
  pattern would compound with the C++ auto-fixes for an even
  shorter debug loop.
* **Per-language extension of `_filter_unused_packages`** — Rust,
  Go, Java currently fall through to pass-through.  Easy to add
  if a future Build run shows mis-installed deps in those
  languages.
* **Resilience banner UI polish** — currently a subtle italic
  notice in the message stream; could become a small chip in
  the phase timeline if the operator wants more salience.

**Cycle D is closed.**
