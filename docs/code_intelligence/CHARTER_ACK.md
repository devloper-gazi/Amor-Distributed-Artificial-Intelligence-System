# Charter Acknowledgement (§11.First)

**Build:** AMOR Code Intelligence Mode v2
**Branch:** `feat/code-intelligence-mode-v2`
**Charter version:** 1.0
**Master Prompt version:** v2.0
**Acknowledged at:** 2026-04-27

## What the Charter requires of me

I am the construction agent for a multi-agent system that other engineers
will extend, that real users will depend on, and that **executes
untrusted code**. The Charter elevates the cost of cutting corners: a
sandbox escape, a silent data corruption, or a wrongly-built seam costs
hours of rework on every subsequent step. My operating mode is
**slow planner, deliberate executor, ruthless verifier**.

I have full authority on routine engineering decisions. I escalate only
when (a) the Master Prompt's constraints can't be satisfied, (b) I
would have to modify code outside the Code Intelligence boundary, or
(c) I hit a security trade-off the Master Prompt doesn't address.

## What the Master Prompt requires of me (compressed)

A new first-class mode in AMOR for code generation, debugging, review,
refactoring — running 100% on local Ollama models. Architecturally
equivalent to Analyze & Think mode but with five specialist agents,
execution grounding via Docker sandbox, an auto-pulling model
registry, autonomous capability discovery (HF / GitHub / arXiv),
adversarial-event filtering, and full observability via OpenLLMetry
or JSONL fallback. Zero paid APIs. Permissive licensing only.
Sandbox containment with `--network none`, `--read-only`,
no-new-privileges, hard CPU + memory caps. Per-session ownership
checks. Idempotent persistence. Cancellation propagating in ≤3s.

## What I have done so far

Eleven commits on `feat/code-intelligence-mode-v2` covering:
pre-flight inventory (4 docs), four new backend modules with 42
tests passing, integration into routes + lifespan + frontend +
infra, six docs (architecture / changelog / runbook / capabilities /
license-notes / ADR-0001 / session-log).

## What I have NOT done that the Charter expects

I owe the user an honest audit before I proceed. Per §9 Definition of
Done, partial completion is not completion. I will produce that audit
in `DEFINITION_OF_DONE_AUDIT.md` and then propose a path to closure.

## My commitments going forward

1. **Pre-flight is permanent.** I will not write code in this build
   without first re-reading the relevant existing-codebase pattern.
2. **Quality gates run after every commit.** All seven of Charter §5,
   not just the green ones.
3. **Atomic commits.** One concern per commit, conventional message,
   `feat(code):` / `test(code):` / `fix(code):` / `docs(code):` scope.
4. **Test-first.** Failing test first, then minimum implementation,
   then refactor under the test.
5. **Resource lifecycle in `try/finally`.** Every container, every
   tempdir, every subprocess.
6. **No silent failures.** Every `except` re-raises with logged context,
   transforms to typed result, or returns typed sentinel with WARNING.
7. **Configuration over hard-coding.** New tunable values land in
   `Settings`, not scattered constants.
8. **Dependency injection at the engine seam.** No direct imports of
   `call_ollama` inside the engine module — always injected.
9. **Documentation alongside code.** CHANGELOG / ARCHITECTURE updated
   in the same commit that introduces the change.
10. **Three-strike failure recovery** with documented escalation if
    structural issues recur.

I have read the Charter end-to-end. I understand that "completed"
means seven gates green, eighty-five-percent coverage, every
Definition-of-Done bullet ticked, and a clean-clone-to-demo path
that works without manual intervention. I will not announce
completion until that bar is met.
