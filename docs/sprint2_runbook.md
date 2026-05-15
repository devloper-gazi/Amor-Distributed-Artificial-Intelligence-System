# Sprint 2 v18 — Property critic + branch coverage Reflexion runbook

> Cycle F Sprint 2 — Hypothesis property-based testing + pytest-cov
> branch-coverage signal feeding the Reflexion loop's coder retry.
> Companion to `docs/sprint1_runbook.md`.

## What landed (this sprint, all offline-tested)

### Sandbox (Python TEST_RUNNERS)

* `document_processor/code_intelligence/sandbox.py`:
  * `test_install_prefix` now also installs
    `pytest-cov`, `coverage`, and `hypothesis` (~2 MiB + 5 s on
    first install; cached afterwards in `/tmp/pip-test-prefix`).
  * `test_cmd` adds
    `--cov=. --cov-branch --cov-report=json:.coverage.json`.
  * `ExecutionResult` gains a new optional field `coverage_json:
    Optional[Dict[str, Any]]` — the sandbox harvests
    `.coverage.json` from the workdir BEFORE `shutil.rmtree`.

### Coverage reader

* `document_processor/code_intelligence/coverage_reader.py` — NEW.
  Pure-stdlib parser for the coverage.py JSON shape.  Surfaces:
  * `BranchCoverageReport` dataclass with `branch_coverage_ratio`,
    `line_coverage_ratio`, `num_branches`, `covered_branches`,
    `missed_branches: list[MissedBranch]`.
  * `parse_coverage_json(dict) -> BranchCoverageReport` — accepts a
    parsed dict.
  * `load_coverage_from_workdir(path) -> BranchCoverageReport` —
    finds + reads `.coverage.json` from a workdir (alternative path
    when the dataclass field isn't used).
  * `format_missed_branches_block(report, threshold) -> str` —
    renders a coder-facing MISSED_BRANCHES feedback block; returns
    empty string when coverage is at/above threshold.

### Tester agent — property mode

* `document_processor/code_intelligence/prompts.py:tester_prompt(...)`
  gains `property_mode: bool = False`.  When True and the target
  language is Python, the prompt injects a "PROPERTY-BASED
  REQUIREMENTS" directive instructing the tester to write at least
  2 `@given` invariant tests in addition to example-based tests.
  No-op on non-Python (Hypothesis is Python-only).
* `document_processor/code_intelligence/agents.py:TesterAgent`
  gains a `property_mode` constructor flag.  Output `.data` carries
  two new fields:
  * `property_mode: bool` — the flag the agent ran with.
  * `property_tests_present: bool` — heuristic check via `@given`
    string scan; cheap, false-positive-resistant.

### Engine wiring

* `document_processor/code_intelligence/engine.py`:
  * `self.test_metadata: dict` + `self.coverage_report: Any` — new
    attributes initialised in `__init__`.
  * `_phase_test` constructs `TesterAgent` with `property_mode=
    settings.code_property_tests_enabled` (default True).
  * After `sandbox.execute(test_mode=True, ...)`, parses
    `test_result.coverage_json` via `parse_coverage_json` and emits
    a `coverage_report` SSE event with branch/line ratios + missed
    branch count.
  * `_score_candidate` breakdown dict gains `property_tests`,
    `branch_coverage`, `line_coverage`, `missed_branches` (all
    INFORMATIONAL — they do NOT alter the 35+25+15+25=100 numeric
    score from Cycle D).
  * `_maybe_run_reflexion` feedback bundle injects
    `MISSED_BRANCHES:` block when branch coverage is below the
    configured threshold AND missed branches exist.

### New settings

* `document_processor/config/settings.py`:
  * `code_property_tests_enabled: bool = True`
  * `code_branch_coverage_threshold: float = 0.80`

### Tests

| file | tests |
|---|---|
| `tests/code_intelligence/test_coverage_reader.py` | 17 |
| `tests/code_intelligence/test_property_critic.py` | 9 |

26 new tests, all green (`pytest tests/code_intelligence/test_coverage_reader.py tests/code_intelligence/test_property_critic.py -v`).

### Gate snapshot

```
$ pytest tests/code_intelligence/test_coverage_reader.py \
         tests/code_intelligence/test_property_critic.py \
         tests/setup \
         tests/api/test_sse_single_replica.py \
         tests/baselines -q
133 passed, 1 warning in 1.75s

$ python -m tools.setup verify
[v] All 7/7 verification checks passed.
```

Pre-existing failures in `tests/code_intelligence/test_dependency_forwarding.py`
and `tests/code_intelligence/test_html_routing.py` are unrelated —
those tests were broken before Sprint 2 (the second file is even still
untracked in git).  They fail because `_filter_unused_packages`
(Cycle D dependency hygiene) drops deps not actually imported in the
test code, while the tests assume bare-pass-through.  Separate cleanup
task.

## Sprint 2 exit criteria status

| # | criterion | status |
|---|---|---|
| 1 | Property critic activates on >=80% of Python function tasks in Sprint-0 corpus | needs live Sprint-0 run (gated by operator A/B baseline) |
| 2 | Branch-coverage signal demonstrably triggers successful self-correction on >=30% of initially-failing tasks | needs live Sprint-0 run |
| 3 | Total CI test sweep delta: +15 tests, all green | **landed (+26 tests)** |
| 4 | Scorecard committed to `tests/baselines/sprint2_results.json` | depends on Sprint-0 run |

Items #1, #2, #4 are tied to the same overnight Sprint-0 baseline run
the operator schedules at Sprint 1 exit; they get measured at the
same time.  All in-code mechanics are landed and unit-tested.

## How to use it (developer flow)

```bash
# Default: property mode ON for Python, OFF elsewhere
docker compose up -d
# A Build session that targets Python automatically gets:
#   - tester writes @given invariants + example-based tests
#   - pytest runs with --cov=. --cov-branch
#   - reflexion retry receives MISSED_BRANCHES block if coverage < 80%

# Disable property mode (flag-flip rollback):
export AMOR_CODE_PROPERTY_TESTS_ENABLED=false
docker compose restart app

# Tighten branch coverage threshold for premium deliverables:
export AMOR_CODE_BRANCH_COVERAGE_THRESHOLD=0.95
docker compose restart app
```

## Live-verifying after deploy

```bash
# In an AMOR shell:
docker compose restart app
python -m tools.setup verify        # 7/7 ✓

# Then run a Build session that produces Python code; inspect the
# /api/code/diagnostics endpoint payload for:
#   * coverage_report event in the SSE stream
#   * property_tests_present: true in the test_ready event metadata
```

## Rollback

| change | rollback |
|---|---|
| Property mode | `code_property_tests_enabled=false` setting / env |
| Branch coverage feedback | `code_branch_coverage_threshold=0.0` setting / env (always above threshold) |
| Sandbox pytest-cov install | revert `sandbox.py:TEST_RUNNERS["python"]` to pre-Sprint-2 prefix |
| Coverage JSON harvest | revert `sandbox.py` `coverage_payload` block |

No DB migration, no schema change, no breaking API.  Every revert is
one settings/env flip or one code revert.

## Caveats

* **`property_tests_present` is a heuristic** — checks for `@given`
  string in the tester output.  False negatives possible if the
  tester defines `@given` via `from hypothesis import given as
  _given` (rare, gracefully degrades to False).
* **`code_branch_coverage_threshold` is informational** — branch
  coverage is a feedback signal to the coder retry, not a hard
  gate.  Quality score is unchanged by coverage.
* **Hypothesis adds ~5 s to first sandbox run** for the pip install
  step; subsequent runs reuse the cached `/tmp/pip-test-prefix`.
* **Pre-existing dependency-forwarding test failures** (7 tests in
  `test_dependency_forwarding.py` + `test_html_routing.py`) are
  unrelated.  Cleanup pending.
