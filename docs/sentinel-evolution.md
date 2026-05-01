# Sentinel — Phase 15: Evolution Engine

Phase 15 turns Sentinel from a *fixed* multi-agent security pipeline
into a *self-improving* one.  Every layer of the V1 pipeline can
mutate, be evaluated, and (with operator consent) replace its current
production version.

The whole system is **gated by a Merkle-chained immutable ledger** —
nothing changes production without leaving a tamper-evident trail,
and every mutation runs through the same constraint check before it
can land.

License: MIT.

---

## Subsystems

| ID | Name | Module | What it learns |
|----|------|--------|----------------|
| A | Preference logging | `preferences.py` | DPO pairs from operator decisions |
| B | QLoRA fine-tuning | `lora_pipeline.py` | Per-agent adapter weights |
| C | Prompt evolution | `prompt_evolution.py` | System-prompt versions per role |
| D | Rule synthesis | `rule_synthesis.py` | Custom Semgrep rules from real findings |
| E | Agent spawning | `agent_spawning.py` | New specialist roles for hot CWE clusters |
| F | Knowledge distillation | `distillation.py` | Smaller fast students for easy cases |
| G | Curriculum self-play | `curriculum.py` | Vygotsky-style level progression |
| H | DAG mutation | `dag_mutation.py` | Pipeline topology variations |
| I | Governance | `governance.py` | Constraints + ledger + sandbox dirs |

Each subsystem is independently testable, ledger-aware, and gated by
its own settings flag.  Disabling one (e.g. `lora_pipeline_enabled =
False`) does not affect the rest.

---

## Governance layer (Subsystem I)

Three primitives shared by every subsystem:

### `LedgerStore`

Append-only JSONL at `<root>/ledger.jsonl` with a SHA-256 hash chain.

* `append(actor, kind, payload)` — writes a new entry whose
  `parent_hash` is the previous tail.
* `entries()` — chronological list.
* `verify()` — re-hashes the chain end-to-end; returns `False` on
  any tamper.
* `tail_hash` (property) — current head of the chain.

Allowed `kind` values are a fixed Literal: `prompt_promoted`,
`prompt_rolled_back`, `adapter_promoted`, `lora_rolled_back`,
`rule_promoted`, `agent_spawned`, `agent_promoted`, `agent_archived`,
`dag_promoted`, `dag_rolled_back`, `student_promoted`,
`constraint_check_failed`, `manual_trigger`, plus a small set of
internal lifecycle kinds.

### `ImmutableConstraints`

A frozen dataclass loaded from `evolution/immutable_constraints.yaml`.
Includes:

* `forbidden_keywords` — substrings that can never appear in a
  promoted prompt / rule / agent manifest (e.g. `rm -rf`,
  `eval(`, `exec(...)`, `dangerouslySetInnerHTML`).
* `output_forbidden_phrases` — phrases that must never appear in
  agent output.
* `network_allowed_hosts` — explicit allow-list (only `localhost`
  and `127.0.0.1` by default).
* `protected_files` / `protected_settings` — filesystem and config
  paths that cannot be touched by any mutation payload.
* `precision_floor` (default 0.6) and `recall_floor` (default 0.3)
  — minimum quality bar for promotion.

`.check(payload)` walks every nested string in the payload (via
`_walk_strings`) and raises `HardConstraintViolation` on any
match.  The route layer wraps every mutating endpoint with this
check and records a `constraint_check_failed` ledger entry on
violation.

### `sandbox_dir(root, label)`

Context manager that creates a temporary directory tree per training
/ evaluation step.  On clean exit the tree is removed; on exception
it is *kept* for forensic inspection.  Used by `lora_pipeline.train_dpo`
and `distillation.train_student` so partial / failed runs never leak
into the production adapter library.

---

## Preference logging (Subsystem A)

`PreferenceStore` writes append-only JSONL plus a SQLite indexed view.
Every operator decision against a Sentinel finding (accept / reject /
edit / silence) becomes a `PreferencePair`:

```python
PreferencePair(
    scan_id=..., agent_name="auditor", user_action="accept",
    chosen={"reasoning": ..., "verdict": "true_positive"},
    rejected={"reasoning": ..., "verdict": "false_positive"},
    code_hash=..., file_hash=..., ast_shape_proxy=...,
)
```

Privacy-by-default: the raw code is **never** stored — only a
SHA-256 hash, a per-file hash, and an AST-shape proxy.  An opt-in
`log_raw_code=True` flag exists but defaults off.

`export_dpo_dataset(agent_name, path)` emits the standard DPO
`(prompt, chosen, rejected)` JSONL format consumed by Subsystem B.

---

## QLoRA fine-tuning (Subsystem B)

`AdapterStore` keeps per-agent versioned manifests at
`<root>/adapters/<agent>/<version>.{yaml,bin}`.

```python
LoRAOrchestrator.train_and_evaluate(
    parent_version=..., preferences_path=..., eval_cases=...,
)
```

* Detects the active backend: `unsloth` → `peft` → `stub`.  Stub
  writes a placeholder so the orchestration logic is fully
  testable without GPU.
* Trains in a `sandbox_dir`.
* Evaluates against `EvalCase` list with the user-provided scorer
  → `EvalResult(precision, recall, f1, latency_ms)`.
* Rejects below the constraint-floor (precision ≥ 0.6, recall ≥
  0.3); records `lora_rejected_low_quality`.
* Promotes only on Pareto improvement vs. the parent (≥ 5%
  precision *or* ≥ 5% recall, with the other not regressing >
  1%); records `adapter_promoted`.
* `rollback(agent, version)` flips the production pointer back
  to a known prior version; records `lora_rolled_back`.

Hardware target: RTX 4060 8 GB.  4-bit quantization, batch_size=1,
grad_accum=16, seq_length=2048.  Training is fully optional —
sentinel works without it; the orchestrator just stays idle.

---

## Prompt evolution (Subsystem C)

`PromptStore` keeps `<agent>/<version>.yaml` manifests with status
`production` / `staging` / `archived`.

Three mutation mechanisms, layered:

1. **DSPy-lite few-shot bootstrap** — sample N successful past
   findings, inject them into the prompt as inline examples, run
   `evaluate_prompt`.
2. **Genetic mutation** — small LLM call paraphrases the prompt;
   fence stripping handles the inevitable Markdown wrapping.
3. **Adversarial addendum** — when the prompt fails on a known
   exploit, append a "watch out for X" clause.

`PromptEvolutionEngine.run_generation(...)` runs the three in
sequence, scores every candidate, applies the precision/recall
floor, then `is_pareto_improvement()` (eps-based comparison,
strict improvement required) before any candidate is written
to staging.

Promotion is **never automatic** — staging items wait for an
explicit operator approval (Console UI / `/promote` endpoint).

---

## Rule synthesis (Subsystem D)

When the same finding pattern recurs ≥ 3 times across scans,
`RuleSynthesizer.synthesize_for_group()`:

1. Groups examples by `(cwe, language)`.
2. Calls a small LLM with the `RULEWRITER_SYSTEM_PROMPT` →
   Semgrep YAML.
3. Strips Markdown fences.
4. `constraints.check(rule_yaml)`.
5. Shadow-validates against `historical_findings`:
   * `precision = matches ∩ confirmed_tp / matches`
   * `recall    = matches ∩ confirmed_tp / total_tp`
6. Promotes only when `precision ≥ 0.9` AND `recall ≥ 0.5`.

`retire_underperforming(window_days=60)` archives production rules
whose precision drops below 0.7.

---

## Agent spawning (Subsystem E)

`MetaMonitor` watches a sliding window of findings.  When one CWE
exceeds `threshold_percent` (default 30%), it emits a
`SpawnRecommendation`.

`AgentFactory.spawn(rec, parent_system_prompt, cwe_corpus_entry,
llm)`:

1. Calls a small LLM with `SPAWN_SYSTEM_PROMPT` to write the
   specialist's system prompt.
2. `constraints.check(prompt)` — blocks forbidden phrases.
3. Writes `<root>/spawned_agents/<name>/manifest.yaml` with
   `status="shadow"`.
4. Records `agent_spawned` ledger entry.

`ShadowTracker` logs per-finding (parent vs. spawned) decisions
in JSONL.

`AgentPromoter.maybe_promote(agent)` waits `shadow_days` (default 30),
then:

* If `(spawned_correct - parent_correct) / parent_correct ≥
  improvement_percent` → `agent_promoted` (status → active).
* Else → `agent_archived` (status → dormant).

---

## Knowledge distillation (Subsystem F)

Two pieces:

1. `EasyCaseRouter` — heuristic gate.  Routes a finding to a fast
   student when:
   * `vote_variance ≤ 0.15` (agents agree)
   * `cwe_rarity ≤ 0.65` (well-known pattern)
   * `file_complexity ≤ 0.6` (small / simple file)
   * `confidence ≥ 0.7` (Bayesian merge confident)
   Otherwise → full pipeline.

2. `DistillationOrchestrator` — collects teacher
   (Judge / Auditor) outputs into `DistillationCorpus`, exports
   an SFT dataset `(prompt, completion)` JSONL when
   `corpus.count() ≥ trigger_rows` (default 5 000), and trains a
   smaller student (phi-3.5-mini / qwen2.5:1.5b) via the same
   peft / unsloth / stub backend as Subsystem B.

---

## Curriculum self-play (Subsystem G)

`LeveledRecipe` defines four difficulty levels per CWE:

* L1 — bare patterns (hardcoded keys, plain SQLi, plain `eval`).
* L2 — disguised (SQLi via helper, cmd injection via `os.popen`).
* L3 — race conditions, subprocess shell injection, length-
  validated `eval`.
* L4 — polyglots, prompt-injection comments, SSRF via DNS
  rebinding.

`CurriculumStore.update_pass_rate(cwe, level, passed, total)`:

* `≥ 0.95` → next level (`PROMOTE_THRESHOLD`).
* `< 0.50` → previous level (`DEMOTE_THRESHOLD`).
* Capped at level 4.

Used to keep the agents in their Vygotsky zone of proximal
development.

---

## DAG mutation (Subsystem H)

`DEFAULT_DAG`: 12-node pipeline:

```
static_swarm → ml_pipeline ──┐
                              ▼
                          aggregate ─→ rag_enrich ──┬─→ auditor   ──┐
                                                    ├─→ reasoner  ──┤
                                                    └─→ redteam   ──┤
                                                                    ▼
                                                                 patcher
                                                                    │
                                                            critic_loop
                                                                    │
                                                                  judge
                                                                    │
                                                                  score
                                                                    │
                                                                 report
```

Five mutation operators, every one with safety guards:

| Operator | Guards |
|----------|--------|
| `add_edge` | self-loop blocked, unknown nodes blocked, cycle blocked |
| `add_node` | duplicate label blocked; downstream re-wired |
| `bypass_node` | requires both incoming and outgoing edges |
| `parallelise` | `n ≥ 2` |
| `swap` | only when neighbourhoods identical |

Replay test: `run_replay(dag, cases, scorer)` runs the candidate DAG
against historical scan inputs and returns `ReplayMetric(precision,
recall, elapsed_ms)`.

`is_pareto_dag_improvement(candidate, baseline)` — 3-axis Pareto with
a default 10% latency-tolerance band so a small slowdown that buys
real precision wins is still a Pareto move.

`DAGMutator.propose_generation(...)` builds candidates without
promoting; `DAGMutator.promote(proposal)` requires explicit user
consent and records `dag_promoted`.

---

## Operator surface — Evolution Console

Routes are mounted at `/api/sentinel/evolution/*` (see
`document_processor/api/sentinel_evolution_routes.py`):

| Method | Path | Purpose |
|--------|------|---------|
| GET  | `/health` | Liveness + chain integrity |
| GET  | `/genome` | Current production state snapshot |
| GET  | `/ledger?limit&offset&kind&actor` | Chain entries (paginated) |
| GET  | `/proposals` | Every staging-state mutation |
| GET  | `/stats` | Aggregate counts per subsystem |
| POST | `/promote` | Promote one staging item (constraint-checked) |
| POST | `/rollback` | Flip production pointer (DAG / prompt / adapter) |
| POST | `/trigger/{sub}` | Queue a manual subsystem run |

Every mutating endpoint:

1. `_check_enabled()` — bails if `sentinel_evolution_enabled = False`.
2. Looks up the target dataclass by id.
3. `constraints.check(payload)` — appends `constraint_check_failed`
   on violation and returns 400.
4. Calls the underlying `Store.promote()` / `rollback_to()` method.
5. Appends a `*_promoted` / `*_rolled_back` / `manual_trigger`
   ledger entry.

The Console UI (`web_ui/static/js/sentinel-evolution.js`) is a
self-contained vanilla-JS modal:

* Five tabs: Genome / Ledger / Proposals / Trigger / Rollback.
* Polls `/health` + `/genome` + `/ledger` + `/proposals` + `/stats`
  every 10 s.
* Ledger rows are kind-color-coded (promote = green, rollback =
  amber, constraint_check_failed = red, manual_trigger = blue).
* Promote button on each proposal opens a confirmation prompt with
  optional note.
* Rollback form takes (kind, agent / pipeline label, target version,
  optional note) and confirms before firing.

The trigger-tab dropdown only contains subsystems for which the
matching `sentinel_evolution_allow_*_trigger` flag is `True` on the
server.

---

## Configuration

```python
# document_processor/config/settings.py
sentinel_evolution_enabled: bool = True
sentinel_evolution_root: str = "data/sentinel/evolution"
sentinel_evolution_actor_default: str = "console"
sentinel_evolution_max_ledger_page: int = 500
sentinel_evolution_require_user_consent: bool = True

sentinel_evolution_allow_prompt_trigger: bool = True
sentinel_evolution_allow_rule_trigger: bool = True
sentinel_evolution_allow_spawn_trigger: bool = True
sentinel_evolution_allow_dag_trigger: bool = True
sentinel_evolution_allow_lora_trigger: bool = False    # opt-in
sentinel_evolution_allow_distill_trigger: bool = False # opt-in
sentinel_evolution_allow_curriculum_trigger: bool = True
```

LoRA and distillation triggers are off by default because they
launch real GPU training.  Operators flip them on per-host.

---

## Disk layout

```
<sentinel_evolution_root>/
├── ledger.jsonl                              # Subsystem I
├── immutable_constraints.yaml
├── prompts/
│   └── prompts/<agent>/<version>.yaml        # Subsystem C
├── adapters/
│   └── adapters/<agent>/<version>.{yaml,bin} # Subsystem B
├── synthesized_rules/
│   ├── staging/<rule_id>.yaml                # Subsystem D
│   ├── production/<rule_id>.yaml
│   └── archived/<rule_id>.yaml
├── spawned_agents/
│   └── <name>/manifest.yaml                  # Subsystem E
├── distillation/
│   ├── corpus.jsonl                          # Subsystem F
│   └── students/<name>/<version>.yaml
├── architecture/
│   └── dag_<version>.yaml                    # Subsystem H
├── preferences.jsonl                         # Subsystem A
├── preferences.sqlite
└── curriculum.jsonl                          # Subsystem G
```

---

## Test surface

| Subsystem | Tests | File |
|-----------|-------|------|
| Governance + preferences | 29 + 14 | `test_governance.py`, `test_preferences.py` |
| Prompt + rule synth | 32 | `test_prompt_evolution.py`, `test_rule_synthesis.py` |
| Agent spawning + curriculum | 24 | `test_agent_spawning.py`, `test_curriculum.py` |
| LoRA + distillation | 19 | `test_lora_distillation.py` |
| DAG mutation | 22 | `test_dag_mutation.py` |
| Routes (Console API) | 25 | `test_routes.py` |
| **Phase 15 total** | **151** | `tests/sentinel/evolution/` |

Plus 200 V1 tests under `tests/sentinel/`.  Phase 15 sweep:
`pytest tests/sentinel -q` → **351 passing**.

---

## Failure modes & rollback

| Issue | Mitigation |
|-------|------------|
| Ledger tampered | `LedgerStore.verify()` returns False; `/health` flips status pill to "ledger TAMPERED" |
| Constraint violation in promote | 400 + `constraint_check_failed` ledger entry |
| Adapter regresses | Pareto gate refuses; `lora_rejected_low_quality` recorded |
| Promoted DAG misbehaves | `/rollback` to a prior version; one ledger entry |
| Subsystem misbehaves entirely | flip `sentinel_evolution_enabled = False` and restart |
| LoRA training crashes | `sandbox_dir` keeps the broken tree for forensics; production untouched |

Hard rollback: revert the Phase 15 commits.  Subsystem B / F do not
share state with V1 — disabling them returns Sentinel to the V1
fixed pipeline.
