# AMOR v18.0.0 — Cycle F release notes (DRAFT TEMPLATE)

> **Status**: template — fill in once the v18 launch gate passes
> end-to-end on a fresh release candidate.  Replace placeholders
> tagged `<…>` before tagging.

## Headline

AMOR v18 closes Cycle F with the inference layer migrated from
Ollama to llama.cpp + llama-swap, three production-quality
verification surfaces (Hypothesis property critic, branch-coverage
Reflexion signal, MCP-tool approval flow), the foundations for
LoRA-specialised role adapters, and Anthropic Agent Skills as the
default deliverable-templating layer.

Six sprints, ~250 days of effort compressed into ~10 days of
focused work, ending with a five-condition launch acceptance gate
that any future release candidate must clear before tag.

## Sprint summary

| # | Sprint | User-visible win |
|---|---|---|
| 1 | Inference migration (Ollama → llama.cpp + llama-swap) | Faster first-token latency (cache-reuse 7.7× on warm prefix); per-session KV-cache continuity; A/B-tested KV quant settling on Q8_0 (correctness +1.23 vs Q4_0). |
| 2 | Property critic + branch coverage Reflexion | Hypothesis @given invariants in every Python tester output; missed-branch feedback to the coder retry; new informational fields in the quality breakdown without changing the 35+25+15+25 score weights. |
| 3 | LoRA hot-swap runtime | Per-request adapter switching (~1-10 ms) instead of full-model llama-swap rotation (~3.5 s); ORPO-on-Qwen2.5-Coder recipe baked in. |
| 4 | Anthropic Agent Skills | 8 production skills (snake game, todo, landing, dashboard, REST API, CLI, data viz, blog) + frontmatter-only planner-prompt index; budget-aware truncation. |
| 5 | Approval flow + sandbox hardening | Every MCP tool dispatch passes through `ApprovalPolicy.decide()`; inline browser approval card on PROMPT decisions; Wrong #2 fix (Docker socket proxy as default); 20-prompt red-team test gating 100% of destructive ops. |
| 6 | Async pipeline + ORPO weekly cron + v18 launch acceptance gate | Test phase joins the existing `gather(execute, analyze)` group → ~30 s saved on the Sprint-0 corpus median; weekly cron + atomic adapter swap helper. |

## Launch acceptance gate (v18 conditions)

All five must hold simultaneously.  Re-run via
`python tools/run_v18_launch_gate.py`.

| # | Condition | Threshold | Measured (run `<UTC>`) |
|---|---|---|---|
| 1 | Sprint-0 correctness mean | ≥ 7.2 / 10 | `<value>` ✓/✗ |
| 2 | HumanEval+ pass@1 | ≥ 72% | `<value>` ✓/✗ |
| 3 | SWE-bench-Lite-25 resolved | ≥ 28% | `<value>` ✓/✗ |
| 4 | Pipeline median latency | ≤ 75s | `<value>` ✓/✗ |
| 5 | Deliverable rubric pass rate | ≥ 70% | `<value>` ✓/✗ |

## Locked decisions (do not relitigate)

* Coder/tester/debugger base: **Qwen2.5-Coder-7B-Instruct Q4_K_M**
* Planner: **DeepSeek-R1-0528-Qwen3-8B Q4_K_M**
* Out-of-family critic: **Phi-4-14B Q4_K_M on CPU, async-only**
* Inference engine: **llama.cpp** pinned `b8500-b8700` + llama-swap
* KV quant: **`-ctk q8_0 -ctv q8_0`** (Sprint 1 A/B winner)
* Speculative decoding + EAGLE-3: **OFF**
* Embedder: **BGE-M3** (dense+sparse) on GPU
* Reranker: **BGE-Reranker-v2-M3** on CPU
* Vector DB: **LanceDB**
* Browser MCP: **microsoft/playwright-mcp + browser-use**
* Verification: **Hypothesis in-loop, mutmut v3 nightly, CrossHair gated**
* Skills format: **Anthropic Agent Skills** (agentskills.io)

## Rollback

Every Cycle F feature is OFF by default OR flag-flip-revertable.
Settings env-vars:

| change | rollback flag |
|---|---|
| Sprint 1 (llama-swap) | `AMOR_LLM_BACKEND=ollama` |
| Sprint 2 (property critic) | `AMOR_CODE_PROPERTY_TESTS_ENABLED=false` |
| Sprint 3 (LoRA hot-swap) | `AMOR_CODE_LORA_ENABLED=false` |
| Sprint 4 (Agent Skills) | `AMOR_CODE_SKILLS_ENABLED=false` |
| Sprint 5 (approval flow) | `AMOR_CODE_APPROVAL_ENABLED=false` |
| Sprint 5 (Wrong #2 proxy) | `AMOR_DOCKER_HOST=` (empty in .env) |
| Sprint 6 (async pipeline) | `AMOR_CODE_PIPELINE_PARALLEL=false` |
| Sprint 6 (critic warmup) | `AMOR_CODE_CRITIC_PREFIX_WARMUP=false` |

## Operator runbooks

* `docs/setup_system.md` — cross-platform install (`./setup.sh install`)
* `docs/sprint1_runbook.md` — Sprint 0 baseline + KV-quant A/B
* `docs/sprint2_runbook.md` — Property critic + branch coverage
* `docs/sprint3_runbook.md` — LoRA hot-swap + ORPO training
* `docs/sprint4_runbook.md` — Agent Skills loader + 8 skills
* `docs/sprint5_runbook.md` — Approval policy + SSE bridge
* `docs/sandbox_hardening.md` — Wrong #2 + sandbox flags + runc-via-Docker-Desktop
* `docs/cycle_e_active.md` — Cycle E + F landed-feature tracker

## Tests + verification

* **Python**: 469+ tests across `tests/setup`, `tests/api`,
  `tests/baselines`, `tests/skills`, `tests/code_intelligence`,
  `tests/local_ai`, `tests/training`, `tests/red_team`.
* **Frontend**: 157 vitest tests across `web_ui/v2/src/`.
* **Live verify** (`python -m tools.setup verify`): 7/7 ✓.
* **Sandbox-through-proxy smoke** (`tools/sandbox_proxy_smoke.py`):
  11/11 ✓.
* **20-prompt red-team** (`tests/red_team/test_destructive_ops.py`):
  20/20 destructive ops gated.

## Known caveats

* **Build mode judge fragility** (carried to v19): 3 of 4 Build
  prompts in the Sprint-0 corpus surface "pass-1 score missing"
  on the Mistral judge; this is a judge-protocol issue, not an
  AMOR pipeline issue.  Track 1 §4 redesign deferred.
* **Q4_0 KV quant**: shelved in Sprint 1 A/B; re-evaluate after
  Sprint 3 LoRA adapters land (adapter specialisation may absorb
  the quant noise per the `docs/sprint1_decision.md` re-eval
  triggers).
* **Phi-4 critic role**: GGUF downloaded as Sprint 0 fallback
  judge.  Wiring Phi-4 as the *inference-time* critic (vs the
  judge) is a v19 follow-on; today's pipeline still uses the
  model_registry critic role (qwen2.5:7b via Ollama or
  llama-swap fast model).
* **MessageActions → preference_pairs.jsonl bridge**: the rating
  UI exists from Cycle D; writing the resulting pairs to disk for
  the weekly ORPO cron is a Sprint 6 follow-on.

## Tag command

```bash
git tag -a v18.0.0 -m "AMOR v18 — Cycle F: inference migration + property critic + LoRA + Agent Skills + approval flow + async pipeline"
git push origin v18.0.0
```

## Acknowledgements

Cycle F integrated user-supplied research output (Top-Five
upgrades, three Wrongs, 90-day sprint schedule, Ship-Now
decisions, Deferred bucket, Launch Acceptance Gate) verbatim;
implementation followed the user-confirmed "user-visible first"
sequencing on 2026-05-15.
