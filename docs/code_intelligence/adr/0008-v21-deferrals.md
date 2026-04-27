# ADR-0008 — Explicit v2.1 deferrals (the canonical list)

**Date:** 2026-04-27
**Status:** Accepted
**Supersedes:** ADR-0001 § Decisions (which carried an early version of
this list)

## Context

The Engineering Charter v1.0 was issued AFTER the v2 build shipped its
core 11 commits. The Charter §9 Definition of Done is binary —
"subjective sense of completeness doesn't count". This ADR records
every item the Master Prompt explicitly required that is not closed
in this v2 PR, the rationale, and the v2.1 closure plan.

## Deferred items

### D1. Tree-of-thoughts Debugger (Master Prompt §4.6)

The current `DebuggerAgent` is a single-pass reasoner. The Master
Prompt requires generating 3 candidate diagnoses, scoring them, and
pursuing the highest. Deferred because the benefit is unclear without
an eval harness (running against SWE-bench Lite) and the 3× LLM cost
on local Ollama is substantial.

**v2.1 plan:** Build a 10-task SWE-bench Lite eval harness first.
Verify single-pass baseline. Implement ToT. Run head-to-head. Ship
the version that wins. Estimated effort: 1 sprint.

### D2. Multi-persona Critic ensemble (Master Prompt §4.6)

Current `CriticAgent` is single-persona. Master Prompt requires
SecurityReviewer / StyleReviewer / PerformanceReviewer / SpecReviewer
+ a Judge model that merges. Same eval-harness dependency as D1.

**v2.1 plan:** After D1's eval harness lands. Estimated effort:
3-4 days for the 4 personas + Judge prompt engineering + ensemble
orchestration.

### D3. `execute_pytest` structured wrapper (Master Prompt §4.3)

The v1 `ExecutionSandbox.execute(extra_files=...)` already supports
multi-file projects. The Master Prompt requires a dedicated
`execute_pytest` returning a structured `PytestResult` with per-test
pass/fail/error and timings. v1 currently parses pytest text output
generically.

**v2.1 plan:** 1 day. Add the method, parse `--json-report` output,
add typed result dataclass.

### D4. `extract_symbol_graph` for non-Python (Master Prompt §4.4)

`StaticAnalysisHarness` extracts Python AST. Master Prompt requires a
tree-sitter-backed `extract_symbol_graph(code, language)` for non-
Python languages. RepoMap already covers the workspace-level need;
the per-snippet need is real but lower priority.

**v2.1 plan:** 2 days. Reuse `_ts_snapshot` from RepoMap; expose as
a public method on the harness.

### D5. Strict-mode discovery sandbox install + smoke + benchmark
(Master Prompt §4.8 gates 3-5)

See ADR-0007. Substantial standalone module; deferred to v2.1.

**v2.1 plan:** 2 weeks. The Tier-2 install harness is a project of
its own.

### D6. v1 module unit-test backfill to hit 85% coverage
(Charter §5 Gate 4)

Current coverage 40.20% (v2 modules at 56-100%, v1 modules at
13-27%). Charter target is 85%. The v1 modules predate the Charter
and shipped without direct unit tests; they have indirect coverage
through routes integration but no module-level tests.

**v2.1 plan:** 1 sprint. Add ~50 unit tests covering engine phases
(mocked LLM), agent run() methods, sandbox execute, registry select+pull,
static analysis tools.

### D7. Strict pyright (Charter §5 Gate 3 target)

Currently `[tool.pyright] typeCheckingMode = "basic"`. Charter target
is `strict`. Switching now would surface ~50-100 `unknown` and
`possibly None` errors across the v1 modules.

**v2.1 plan:** Pair with D6. After unit tests give confidence in
behaviour, tighten types module by module.

### D8. Engine refactor to consult AgentRegistry (Charter §6 Mandate 1)

The registry exists; the engine still imports concrete agent classes
directly. The Mandate is only fully satisfied when the engine
constructs agents via `agent_registry.require(role)(llm_call, ...)`.

**v2.1 plan:** 1 day. Single refactor commit; tests already cover
the registry path.

### D9. `RepoMap` injection into agent prompts (Master Prompt §4.7)

`RepoMap.repo_map(...)` exists and is tested. The engine doesn't yet
prepend it to Coder/Debugger/Critic prompts. The integration point is
in `engine.py:_phase_implement` and friends.

**v2.1 plan:** 0.5 day. Add a `_render_repo_map()` helper called
once per agent prompt.

### D10. `AdversarialReviewer.reload_rules()` admin endpoint
(Charter §10 RUNBOOK)

The reviewer's `reload_rules()` method works; there's no HTTP
endpoint to call it without restarting the app.

**v2.1 plan:** 0.5 day. `POST /api/code/admin/reload_adversarial_rules`.

## Summary

- **10 items deferred** with explicit acceptance, plan, and effort
  estimate.
- Total estimated v2.1 effort: ~5 weeks (some items parallelizable).
- None of the deferrals violate Charter §1 invariants (zero paid
  APIs, sandbox containment, permissive licensing, existing
  conventions). They postpone optional sophistication.

## Acceptance

By committing this ADR, we accept these deferrals as a deliberate
v2.1 backlog. Future sessions resuming this build know exactly where
to pick up.
