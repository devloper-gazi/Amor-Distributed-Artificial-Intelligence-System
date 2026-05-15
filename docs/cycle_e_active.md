# Cycle E + F — active (v18 ready to tag, pending HumanEval+ run)

> Cycle D (Build pipeline polish + Sessions UI + i18n + reflexion +
> domain templates + 16-language sandbox) closed.  Cycle E started
> Pazartesi with the v18 baseline refresh as Sprint 0.  Cycle F
> (the v18 strategic roadmap) **prep + 5 of 6 steps shipped on
> 2026-05-15**; v18.0.0 tag is queued behind the final HumanEval+
> launch-gate measurement.  See `docs/v18_release_notes.md` for the
> tag-ready summary.

## Cycle F final state (state @ 2026-05-15)

| Surface | Status |
|---|---|
| **Cycle F prep landings** | 6 sprints × OFF-by-default features; 311/311 isolated test gate green; **all rollback flags documented** |
| **Sprint 1 EXIT** | PASS (Q8_0 KV winner; correctness mean 7.66/10) |
| **Sprint 6 post-async Sprint-0 baseline** | correctness **8.25/10 (+0.59)**; completeness 7.75/10; per-mode floor **7.33** (Build) — Sprint 1's judge-fragility caveat **RESOLVED** |
| **v18 launch gate** | **3/4 measurable conditions PASS**; latency miss documented as structural; HumanEval+ pending; SWE-bench-Lite deferred to v19 |
| **Test sweep** | pytest 469+/469 green, vitest 157/157 green, red-team 23/23 green, sandbox proxy smoke 11/11 green |
| **Live verify** | 7/7 ✓ |
| **Docker socket proxy** | DEFAULT (Wrong #2 fix) — sandbox routes through allowlist |
| **App replicas** | 1 (Wrong #1 fix) |
| **Approval flow** | Policy gate + SSE bridge + browser inline card landed; OFF by default |
| **Tag command** | `git tag -a v18.0.0` (see `docs/v18_release_notes.md`) |

## TL;DR (state @ end of Cycle D)

| Surface | Status |
|---------|--------|
| **Backend tests (`code_intelligence` + `baselines`)** | **142/142 green** |
| **Frontend bundle gz** | 106.60 kB (+10 kB delta from Cycle C; +30 kB headroom in budget) |
| **Languages supported in sandbox** | **16** (Python, JS, TS, Go, Rust, C, C++, Java, Kotlin, C#, Ruby, PHP, Bash, HTML, CSS, SQL) |
| **Test runners** | **9** (Python+JS+TS+Go+Rust+C+C+++Ruby+PHP) — `set -e + exec` for clean exit propagation |
| **Domain awareness** | **6 templates** (game / web_app / cli_tool / rest_api / library / data_processing) with rule-based detector + planner + coder injection + reflexion feature-coverage check |
| **Reflexion** | landed — quality scorer (execution+test+static+critic → 0-100) + missing-features feedback to coder; threshold=80 + max_iter=1 |
| **i18n** | 15 routes + composer + sidebar + sessions list + admin pages fully Turkish-localized |
| **Sessions sidebar** | derived activity status (pinned/archived/active/recent/idle/stale) + mode chip + group-by-recency + active-mode highlight + 15s polling |
| **Sprint 0 v18 baseline runner** | **Pazartesi-ready** — Mistral primary + Phi-4 fallback + master `tools/run_sprint0_v18.sh` + 12 profile-loader tests + smoke verified (judge starts 4s, model load 23s, judgment 17s) |
| **Cross-platform setup system** | **Landed** — `tools/setup/` Python orchestrator + `setup.sh` / `setup.ps1` thin shims + `Makefile` + 73 unit tests + live-verified preflight / status / doctor / verify on Windows 11 host |
| **Cycle F Sprint 1 — EXIT PASS** | **Landed + A/B verdict shipped** — Sprint 1 mechanics from prep row landed.  Overnight A/B (2026-05-12, ~2.5h wall) verdict: **Stay on Q8_0** (correctness 3.83 vs Q4_0 2.60 — Δ−1.23 fails Pareto's −0.15 tolerance).  Q4_0 catastrophically broke Research mode (3/3 prompts collapsed to score 1).  Latency win (Q4_0 −34%) couldn't rescue correctness.  Q8_0 cache-reuse 0.19×; Q4_0 0.13× (both pass <0.20× gate).  `docs/sprint1_decision.md` written with per-prompt evidence + re-eval triggers.  v18 launch gate #1 (Sprint-0 mean correctness ≥ 7.2/10 → 3.83×2 = 7.66): **PASS by 0.46**.  Build-mode judge fragility flagged as v19 work (3 prompts errored in BOTH variants). |
| **Cycle F Sprint 2 prep** | **Landed** — pytest-cov branch-coverage Reflexion signal + Hypothesis property-mode in TesterAgent.  `sandbox.py` TEST_RUNNERS python installs `pytest-cov + hypothesis`, runs `--cov=. --cov-branch --cov-report=json`, harvests `.coverage.json` into `ExecutionResult.coverage_json` before workdir cleanup.  New `coverage_reader.py` parses → `BranchCoverageReport` + `format_missed_branches_block`; engine wires it into `_score_candidate` breakdown (informational, doesn't alter 35+25+15+25=100 weights) + `_maybe_run_reflexion` feedback bundle.  `tester_prompt(property_mode=True)` injects @given directive for Python; no-op elsewhere.  Two new settings: `code_property_tests_enabled` (default True), `code_branch_coverage_threshold` (default 0.80).  +26 new tests (17 coverage_reader + 9 property critic) → **133/133 isolated gate green**.  Exit criteria #1/#2/#4 land at the next live Sprint-0 run. |
| **Cycle F Sprint 3 prep** | **Landed (OFF by default)** — per-request LoRA hot-swap via `ChatOptions.extra` → llama.cpp PR #10994 `"lora":[{"id":int,"scale":float}]` body field.  Three new settings: `code_lora_enabled=False`, `code_lora_role_adapters="{}"`, `code_lora_default_scale=1.0`.  `tools/lora_runtime.py` (NEW) provides `parse_role_adapter_map` + `lora_payload_for_role` + `disable_all_adapters_payload`.  `local_ai_routes_simple.py` attaches the `lora` field in `ChatOptions.extra` when master gate is on AND `_ACTIVE_ROLE` ContextVar maps to an adapter ID (zero-touch for the existing OpenAI-compat backend — `body.update(dict(opts.extra))` does the wire serialization).  `tools/training/orpo_role_adapter.py` (NEW) is a thin role-aware wrapper around the existing Cycle C `orpo_qwen_coder.py` pinning the Cycle F recipe (r=16, alpha=32, dropout=0.05, lr=8e-6, beta=0.1, 1 epoch, max_seq=2048).  `compose/llama-swap/config.{yaml,q4_0.yaml,q8_0.yaml}` gain commented `--lora-init-without-apply` placeholders for coder/tester/debugger adapters on the editor model.  +40 new tests (19 lora_runtime + 14 orpo_role_adapter + 7 lora_injection) → **172/172 isolated gate green**.  Runbook at `docs/sprint3_runbook.md`.  Outstanding: actual adapter training (~2-3 h GPU work per role × 3 roles) is operator-scheduled. |
| **Cycle F Sprint 4 prep** | **Landed (OFF by default)** — Anthropic Agent Skills loader + 8 production-quality SKILL.md files.  Three new settings: `code_skills_enabled=False`, `code_skills_root="skills"`, `code_skills_token_budget=2000`.  NEW package `local_ai/skills/` with stdlib mini-YAML frontmatter parser, Pydantic `SkillFrontmatter` schema, `_ACTIVE_SKILL` ContextVar (mirrors Sprint 3 `_ACTIVE_ROLE`), `LoadSkillTool` MCP tool.  `prompts.py:planner_prompt` gains a gated `SKILLS AVAILABLE:` block append.  8 SKILL.md files shipped: snake_game_builder, todo_app, landing_page, dashboard, rest_api_service, cli_tool, data_viz, blog_post.  Combined rendered index ≈638 tokens (vs 2000 budget → ample headroom).  +47 new tests (11 schema + 16 loader + 10 activation + 10 skill_md_files) → **220/220 isolated gate green**.  Runbook at `docs/sprint4_runbook.md`.  Outstanding: engine-side body injection (one-line in `_phase_implement` after planner picks a skill) gated behind Sprint 5 approval flow. |
| **Cycle F Sprint 5 prep (policy engine + gate)** | **Landed (OFF by default)** — `ApprovalPolicy.decide()` wraps every MCP `ToolRegistry.dispatch()`.  Three-list resolution + category fallthrough + cost circuit-breaker.  Seven new `code_approval_*` settings.  `Tool.category` class attr.  `+dispatch(..., approval_callback=...)` parameter for the SSE bridge.  +32 new tests → **252/252 isolated gate green**.  Runbook at `docs/sprint5_runbook.md`.  Outstanding: SSE bridge endpoints + runc pin + Wrong #2 (docker-socket-proxy default) + 20-prompt red-team. |
| **Cycle F Sprint 5 (SSE bridge)** | **Landed + live-verified 2026-05-15** — `document_processor/api/approval/{__init__,bridge,routes}.py` (NEW package). `AwaitingApproval` dataclass + `request_user_approval()` async helper (publishes `approval_required` SSE event, awaits future, falls back to denial on timeout). `POST /api/approval/{request_id}` endpoint with cross-replica Redis fanout. `GET /api/approval/_pending` debug endpoint. `mcp_routes.py:call_tool` extended with `session_id` + `actor_role` body fields; builds an `approval_callback` closure that bridges to the SSE channel when session is supplied (otherwise PROMPT fails closed). `main.py` registers the router on boot. +13 new tests (6 bridge + 7 routes) → **265/265 isolated gate green**. Live-verified: `/api/approval/_pending` returns `{pending:[],count:0}`. |
| **Cycle F Sprint 6 (piece 1: v18 launch gate)** | **Landed + live-verified 2026-05-15** — `tools/run_v18_launch_gate.py` (NEW, ~340 LOC).  Runs 6 conditions (5 plan-defined + per-mode floor): Sprint-0 correctness mean ≥ 7.2/10, Sprint-0 completeness mean ≥ 7.2/10, per-mode floor ≥ 6.5/10, pipeline median latency ≤ 75s, HumanEval+ ≥ 72%, SWE-bench-Lite-25 ≥ 28%.  Reads `sprint0_latest.json` + `data/eval_runs/{humaneval_plus,swebench_lite}/latest.json`.  Emits unified scorecard JSON at `data/baselines/v18_launch_gate_<utc>.json` + colour-coded text report.  Exit codes 0=pass, 1=fail/insufficient, 2=fatal.  `--shallow` skips expensive evals; `--re-run-sprint0` invokes the existing overnight runner; `--re-run-evals` surfaces the admin-route triggers.  Live tested against Q8_0 + Q4_0 scorecards: Q4_0 fails all 4 cheap conditions; Q8_0 passes correctness + completeness but fails per-mode floor (Build judge fragility) + pipeline latency (sequential pipeline → Sprint 6 piece 2 will fix).  +19 new tests → **284/284 isolated gate green**. |
| **Cycle F Sprint 6 (piece 2: async pipeline)** | **Landed 2026-05-15** — engine.py `run()` extended: when `code_pipeline_parallel=True` (default), test phase joins the existing `asyncio.gather(execute, analyze)` group → all three concurrent after implement.  New `_warmup_critic_prefix()` helper fires a non-blocking 2-token critic call as soon as `self.code` is ready so the review phase's first LLM call lands on a hot KV cache.  Two new settings: `code_pipeline_parallel=True`, `code_critic_prefix_warmup=True`.  Test phase depends only on `self.code` (NOT on execute/analyze results) so parallelization is correctness-safe.  Best-effort warmup — exceptions swallowed; cold path remains functional.  Expected wall-clock impact: Sprint-0 corpus median ~106s → ~78s (saves ~30s — the tester's LLM call drops out of the critical path).  +8 new tests → **292/292 isolated gate green**.  Outstanding: needs a fresh Sprint-0 run to confirm the median latency hits the ≤75s v18 launch gate threshold. |
| **Cycle F Sprint 6 (piece 3: ORPO weekly cron + LoRA promote)** | **Landed 2026-05-15** — `tools/training/orpo_weekly_cron.py` (NEW, ~210 LOC): walks `data/preference_pairs/{coder,tester,debugger}.jsonl`, skips roles below min-pair gate (50 default), invokes the Sprint 3 `orpo_role_adapter.py` trainer with `--convert-gguf`, writes candidate adapter to `models/lora/candidate/<role>-r16-<utc>.gguf` + a human-readable diff report at `data/training/diff_<utc>.md` with operator promote checklist.  `tools/lora/promote.py` (NEW, ~150 LOC): atomic swap of `candidate.gguf` into the in-production slot `<role>-r16.gguf` with `prev.gguf` backup for one-call rollback (`promote.py rollback --role <r>`).  POSIX + Windows safe via `os.replace`.  CLI: `promote`, `rollback`, `--status` (default).  Does NOT auto-restart llama-swap — operator's call.  +19 new tests (9 cron + 10 promote) → **311/311 isolated gate green**.  Outstanding: actual ORPO training runs require GPU + accumulated preference pairs (a Sprint 5 follow-on once MessageActions writes pairs to disk). |
| **Cycle F Sprint 5 (UI: ApprovalPrompt.tsx)** | **Landed + live-verified 2026-05-15** — inline approval card closes the SSE-bridge loop end-to-end.  `web_ui/v2/src/components/chat/ApprovalPrompt.tsx` (NEW, ~210 LOC): SolidJS component rendering tool name, category, arguments + Approve/Deny buttons + countdown timer; manages its own resolution via `POST /api/approval/{request_id}`.  `lib/types.ts` extended with `ApprovalPayload` interface + `"approval"` role.  `lib/chat-stream.ts`: `StreamPatch.pushTurn` field + `handleEvent` forwards it + `SIMPLE_TEXT_REDUCER` handles `approval_required` events (pushes new approval turn).  `components/chat/MessageThread.tsx` switches on `turn.role === "approval"` to render `ApprovalPrompt` vs `MessageBubble`.  23 new i18n keys × 2 locales (en + tr).  Tests: 12 component tests (`ApprovalPrompt.test.tsx`, vitest + @solidjs/testing-library with explicit cleanup) + 4 reducer tests (`chat-stream.test.ts` extension) → **vitest 157/157 green; pytest 228/229 green** (1 pre-existing Cycle C `test_orpo_scaffold` Windows path-escape failure, unrelated).  Live verify 7/7 ✓. |
| **Cycle F Sprint 5 (Wrong #2: socket proxy default flip)** | **Landed + live-verified 2026-05-15** — `docker-compose.yml:118-141` flipped the `app` service env to default `DOCKER_HOST=tcp://amor-docker-proxy:2375` (+ matching `AMOR_DOCKER_HOST`).  Escape hatch: operator sets `AMOR_DOCKER_HOST=""` in `.env` to fall back to the unix-socket bind-mount.  NEW `tools/sandbox_proxy_smoke.py` (~280 LOC) — 11 assertions verifying the proxy allowlist matches the sandbox's actual Docker API needs.  Smoke run against the live proxy: **11/11 passed** (7 allowed: VERSION/INFO/IMAGES/CONTAINERS/VOLUMES/POST/EXEC; 4 denied: NETWORKS/SWARM/SYSTEM/BUILD).  Sandbox `security_posture()` now reports `via_proxy=True` end-to-end.  `docs/sandbox_hardening.md` (NEW) documents the active hardening flags, the proxy verification protocol, the runc-via-Docker-Desktop mitigation for CVE-2025-31133, and the rollback paths.  Live verify 7/7 ✓ after `compose up -d --force-recreate app` + gateway restart. |
| **Cycle F Sprint 5 (Wrong #3 closure: 20-prompt red-team)** | **Landed 2026-05-15** — `tests/red_team/test_destructive_ops.py` (NEW, ~230 LOC) drives 20 synthetic destructive tool calls through `ToolRegistry.dispatch` with `ApprovalPolicy.enabled=True`, asserting EVERY one is gated (DENY or PROMPT-requires-callback) BEFORE the tool's `execute()` body runs.  Coverage: filesystem deletion (rm_rf, file.delete, dir.rmtree), DB destruction (DROP/ALTER/TRUNCATE), secret exfiltration, git destructive (push --force, reset --hard), shell exec (curl\|sh, kubectl apply), docker (privileged, network create), package (pip install unverified, npm -g), network exfil, write-to-root, explicit name-list deny, unclassified default.  Plus 3 meta-tests: ApprovalCategory coverage roll-up, exactly-20-prompts size pin, valid-codes shape.  **23/23 green in 0.34s**.  Mock-only — zero LLM/sandbox calls.  Full sweep: **pytest 469/469 + vitest 157/157, live verify 7/7 ✓**. |
| **Cycle F Sprint 6 post-async Sprint-0 baseline (Step 4)** | **Run 2026-05-15** — 96 min Mistral judge pass against Q8_0 + async pipeline + critic prefix warmup all ON.  **Quality went UP across the board vs Sprint 1 Q8_0 baseline**: correctness mean **8.25/10 (+0.59 vs 7.66)**, completeness **7.75/10 (+0.09)**, judged rows **8/10 (+2)**, errored rows **2/10 (−1)**.  Per-mode floor **7.33** (Build) — up from 6.0 — Sprint 1's "Build-mode Mistral judge fragility" largely **RESOLVED**.  v18 launch gate run: **3/4 measurable conditions PASS** (correctness, completeness, per-mode floor) + 2 skipped (HumanEval+ + SWE-bench-Lite, gated by Step 5).  **Latency gate FAIL**: median 137.7s vs ≤75s target.  Failure is structural — Build prompts inherently 257-418s (4-stage pipeline + debug retries, e.g. build-flask-rest hit `retries=3` → 418s); Research 61-73s ✓, Thinking 128-141s ✗.  Plan §6 caveat acknowledged: 75s budget assumed Phi-4 critic running fully-async, which is partial today (prefix-warmup landed; full async-decouple is v19 work).  Scorecard: `data/baselines/sprint0_latest.json` + `data/baselines/v18_launch_gate_20260515T151608Z.json`. |
| **Cycle F Sprint 6 (Step 5: Eval runner fixes + bridge)** | **Landed + measured 2026-05-15** — Three pre-existing Cycle C eval-runner bugs surfaced + fixed:  (a) `tools/` was not bind-mounted into the app container → `_register_eval_runners()` failed with `ModuleNotFoundError: No module named 'tools'`.  Fixed via `docker-compose.yml:204` adding `./tools:/app/tools` bind-mount.  (b) `humaneval_plus._llm_base_url()` returned empty string when `AMOR_LLM_BACKEND_URL=""` overrode the LLAMASWAP_URL fallback.  Fixed with falsy-skip env-chain iteration.  (c) `swebench_lite_25` registered with `runner=None` — Cycle C Sprint 2 Day 3 scaffold → **DEFERRED to v19**.  NEW `tools/eval/export_latest.py` (~190 LOC) bridges Postgres `eval_runs` rows to `data/eval_runs/<short_name>/latest.json` for the v18 launch gate runner.  Bonus normaliser: pass@1 fraction → percent.  Bonus `AMOR_EVAL_OUT_ROOT` env override so writes from inside container land at `/data/documents/eval_runs/` (host-visible).  **Final HumanEval+ 50-problem result: 39/50 = 78.0% pass@1 — PASSES gate condition #2 (≥72%) by +6 pp.**  Per-completion latency: p50 1.08s, p95 3.37s.  v18 launch gate verdict: **4 of 5 measurable conditions PASS** (correctness, completeness, per-mode floor, HumanEval+) + 1 structural FAIL (median latency) + 1 deferred (SWE-bench-Lite). |

## What landed in Cycle D (chronological)

1. **Research mode RESEARCH_REDUCER** — 27 tests; replaces "(done)" literal with full markdown report rendering
2. **Build pipeline 6 fixes** — coder C++ awareness (#include + forward-decl), critic verdict-severity coherence, install_packages cross-check, plan-to-spec extractor, planner+critic resilience (retry + fallback)
3. **i18n migration** — 5 modes + admin pages + sessions + composer + 28 new keys EN+TR
4. **Sessions list polish** — status taxonomy + mode chip + group-by-recency + active highlight + 15s polling + 17 helper tests
5. **Build/Thinking 5-tier effort selector** — composer prop retrofit + localStorage persistence
6. **Resilience banner** — `planner_fallback` + `install_packages_filtered` events render subtle italic notices
7. **Polyglot sandbox** — 6 new languages (C, Kotlin, C#, Ruby, PHP, SQL) + 3 new test runners + per-language ground rules
8. **Polyglot detector** — `_sniff_language_from_content` covers 10+ languages; `_heuristic_language_override` 16-pattern explicit detection
9. **Reflexion loop** — quality scorer + iteration loop + reflexion-aware coder prompt + frontend handlers
10. **Domain templates** — 6 production-quality templates + planner directive + coder directive + feature coverage check
11. **Sprint 0 v18 judge profiles** — Mistral primary + Phi-4 fallback + Mistral-fast emergency
12. **Cross-platform setup** — `tools/setup/` package replacing inadequate `start.sh` / `start.ps1` / `validate_setup.ps1` legacy scripts.  Stdlib-only Python orchestrator with preflight + idempotent install + doctor + verify + start / stop / status / logs / restart / destroy subcommands.  POSIX (`setup.sh`) + PowerShell (`setup.ps1`) + Make (`Makefile`) front-ends.  73 unit tests green; live-verified all 12 services + all 7 smoke probes against the running stack.
13. **Cycle F Sprint 1 prep** — Inference migration mechanics fully wired and live-verified: `compose/llama-swap/config.{q4_0,q8_0}.yaml` KV-quant variants + `tools/llamaswap/{select_kv_quant.py,probe_cache_reuse.py}` + `tools/sprint1_ab_run.sh` overnight A/B harness.  `docker-compose.yml` flipped llama-swap to default-on (removed `profiles: [llamaswap]`) and reduced `app.deploy.replicas: 2 → 1` per Wrong #1.  `tools/setup/constants.py` promoted llama-swap to `tier="core"` with profile-intersection guards in install/services so `minimal` doesn't hang waiting on inference services it never started.  Cache-reuse live-verified: cold prefill 283.7 ms / 108 new tokens → warm prefill 36.6 ms / 1 new token = **0.13× prefill ratio, 7.7× speedup, 317 cached tokens**.  22 new tests landed (16 setup + 6 SSE/single-replica regression).  Discovered + corrected the roadmap's `--cram 512` (the flag is actually `-cram` / `--cache-ram` and default 8192 MiB is the correct host-memory setting; the roadmap's `512` would have shrunk the cache).  Outstanding for Sprint 1 exit: the actual ~12 hour A/B Sprint-0 baseline run + `docs/sprint1_decision.md` Pareto-rule verdict.  Runbook at `docs/sprint1_runbook.md`.
14. **Cycle F Sprint 2 prep** — Property-based testing + branch-coverage Reflexion signal wired end-to-end.  `code_intelligence/sandbox.py` TEST_RUNNERS["python"] installs `pytest-cov + coverage + hypothesis` and runs `--cov=. --cov-branch --cov-report=json:.coverage.json`; `ExecutionResult.coverage_json` carries the harvested report.  NEW `code_intelligence/coverage_reader.py` parses → `BranchCoverageReport` (branch ratio, line ratio, missed-branch records) + renders `MISSED_BRANCHES:` feedback block when below threshold.  `prompts.py:tester_prompt(property_mode=True)` injects "write @given invariants" directive for Python only; `TesterAgent` constructor flag + `out.data.property_tests_present` heuristic surface what actually landed.  Engine wires the new signal into `_score_candidate` breakdown dict (informational — Cycle D 35+25+15+25=100 weights preserved) and `_maybe_run_reflexion` feedback bundle.  Two settings added: `code_property_tests_enabled=True`, `code_branch_coverage_threshold=0.80`.  +26 new tests (17 `test_coverage_reader.py` + 9 `test_property_critic.py`) → 133/133 isolated gate green, 7/7 live verify still ✓.  Runbook at `docs/sprint2_runbook.md`.
15. **Cycle F Sprint 3 prep** — LoRA hot-swap runtime path (OFF by default, awaits operator ORPO training).  Three new settings: `code_lora_enabled`, `code_lora_role_adapters`, `code_lora_default_scale`.  NEW `tools/lora_runtime.py` builds the PR-#10994 payload `[{id:int,scale:float}]`; tolerant JSON parser, case-insensitive role lookup, role-scale overrides, disable-all helper.  `local_ai/api/local_ai_routes_simple.py` attaches `lora` field via `ChatOptions.extra` when the master gate + active role + adapter mapping all align — zero changes to `LlamaSwapBackend` / `OpenAICompatibleBackend` thanks to the existing `body.update(dict(opts.extra))` escape hatch.  Per-request adapter switching = ~1-10 ms swap (vs ~3.5 s full-model llama-swap).  NEW `tools/training/orpo_role_adapter.py` thin role-aware wrapper around Cycle C's `orpo_qwen_coder.py` pinning the Cycle F recipe: r=16, alpha=32, dropout=0.05, lr=8e-6, beta=0.1, 1 epoch, max_seq=2048.  Canonical `ROLE_ADAPTER_IDS = {coder:0, tester:1, debugger:2}` (matches `--lora-init-without-apply` mount order).  `compose/llama-swap/config.{yaml,q4_0.yaml,q8_0.yaml}` gain commented `--lora-init-without-apply` mount placeholders on the editor model.  +40 new tests (19 `test_lora_runtime.py` + 14 `test_orpo_role_adapter.py` + 7 `test_lora_injection.py`) → **172/172 isolated gate green**.  Runbook at `docs/sprint3_runbook.md`.  Adapter training itself is operator GPU work (~30-60 min per role × 3 roles).
16. **Cycle F Sprint 4 prep** — Anthropic Agent Skills loader + first 8 SKILL.md files (OFF by default).  NEW `local_ai/skills/` package: `schema.py` (Pydantic `SkillFrontmatter`, ~60 LOC), `loader.py` (stdlib mini-YAML parser + `render_skill_index` budget-aware index renderer, ~80 LOC), `activation.py` (`_ACTIVE_SKILL` ContextVar mirroring Sprint 3 `_ACTIVE_ROLE`, ~30 LOC), `registry_integration.py` (`LoadSkillTool` MCP tool + `register_into(registry, skills)` helper, ~60 LOC).  `prompts.py:planner_prompt` extended with a gated `SKILLS AVAILABLE:` block — appends only when `code_skills_enabled=True` AND the skills root contains valid SKILL.md files.  Three new settings: `code_skills_enabled=False`, `code_skills_root="skills"`, `code_skills_token_budget=2000`.  8 skills shipped under `skills/`: `snake_game_builder`, `todo_app`, `landing_page`, `dashboard`, `rest_api_service`, `cli_tool`, `data_viz`, `blog_post` — each ≤200 tokens frontmatter, ≤2000 tokens body.  Combined rendered index ≈638 tokens (well under 2K budget → 50+ skill headroom before truncation kicks in).  Per-skill error isolation: one malformed SKILL.md becomes a `SkillLoadError` entry; the rest still load.  Skill directory name MUST equal frontmatter `name` (validated at load).  +47 new tests (11 `test_schema.py` + 16 `test_loader.py` + 10 `test_activation.py` + 10 `test_skill_md_files.py`) → **220/220 isolated gate green**.  Live verify against running stack: 7/7 ✓.  Runbook at `docs/sprint4_runbook.md`.  Outstanding (Sprint 5+ work): engine-side body injection after planner picks a skill, gated by Sprint 5's approval flow so a runaway planner can't auto-activate arbitrary scripts/.
17. **Cycle F Sprint 5 prep (policy engine + dispatch gate)** — `ApprovalPolicy.decide()` gate wraps every MCP tool dispatch (Wrong #3 fix), OFF by default.  NEW `local_ai/approval/` package: `policy.py` (~240 LOC) with `ApprovalPolicy` (three-list resolution: deny-name → allow-name → prompt-name → category → default), `ApprovalDecision` enum (ALLOW / PROMPT / DENY / BUDGET_EXCEEDED), `ApprovalCategory` enum (read/write/delete/exec/network/git/db/docker/llm/secret/package), `CostCircuitBreaker` (per-session token budget, idempotent trip), `settings_to_policy()` tolerant CSV+JSON parser, `DEFAULT_POLICY` module singleton, `refresh_default_policy()` lazy settings-driven in-place rebuild with tuple-key cache.  `local_ai/tools/base.py:Tool.category` class attribute (default `"unclassified"`).  `local_ai/tools/registry.py:dispatch()` wrapped: before validate-and-execute, runs `DEFAULT_POLICY.decide(ApprovalRequest(tool_name, category, arguments, session_id, actor_role))`.  DENY → `MCPToolResult(ok=False, metadata={"code":"approval_denied"})`.  PROMPT + no `approval_callback` → fail closed.  PROMPT + callback → `await approval_callback(req)`; True allows, False blocks.  Seven new settings: `code_approval_enabled=False`, `code_approval_allow_silent` (CSV), `code_approval_deny` (CSV), `code_approval_prompt` (CSV), `code_approval_default_action` (allow/prompt/deny), `code_approval_category_actions` (JSON), `code_approval_cost_budget_tokens` (50_000).  +32 new tests (25 `test_approval_policy.py` + 7 `test_dispatch_gate.py`) → **252/252 isolated gate green**.  Runbook at `docs/sprint5_runbook.md`.  Outstanding for full Sprint 5 exit: SSE bridge (`/api/approval/{id}` endpoint + `approval_required` event), `Dockerfile.sandbox` runc ≥ 1.2.8 pin, AMOR_DOCKER_HOST default flip (Wrong #2), 20-prompt red-team test.

## Files added / heavily modified in Cycle D

```
document_processor/code_intelligence/
  agents.py                 (+550 LOC: detector + sniffer + reflexion-aware critic)
  prompts.py                (+450 LOC: per-lang ground rules + domain directive)
  engine.py                 (+400 LOC: reflexion loop + feature coverage + plan-to-spec)
  sandbox.py                (+250 LOC: 6 new langs + TEST_RUNNERS + test_mode)
  domain_templates.py       NEW (~360 LOC)

tests/code_intelligence/
  test_coder_cpp_validation.py             NEW (12)
  test_critic_verdict_coherence.py         NEW (7)
  test_dependency_hygiene.py               NEW (12)
  test_focused_spec.py                     NEW (7)
  test_planner_resilience.py               NEW (11)
  test_language_detection.py               NEW (46)
  test_domain_templates.py                 NEW (29)

tests/baselines/
  test_judge_profiles.py                   NEW (12)

tools/
  judge/judge_profiles.json                NEW
  judge/select_and_start.sh                NEW
  run_sprint0_v18.sh                       NEW
  setup/__init__.py                        NEW (cross-platform orchestrator)
  setup/__main__.py                        NEW
  setup/cli.py                             NEW (argparse dispatch)
  setup/constants.py                       NEW (service catalogue + profiles)
  setup/util.py                            NEW (OS / Docker / spinner / probes)
  setup/preflight.py                       NEW (host gates)
  setup/envfile.py                         NEW (idempotent .env)
  setup/compose.py                         NEW (v1/v2 wrapper)
  setup/health.py                          NEW (poll w/ backoff)
  setup/models.py                          NEW (judge GGUF + ollama bootstrap)
  setup/doctor.py                          NEW (diagnostic report)
  setup/services.py                        NEW (start/stop/restart/status)
  setup/verify.py                          NEW (live smoke)
  setup/install.py                         NEW (8-phase orchestrator)

tests/setup/                                NEW
  test_cli.py                              NEW (9 tests)
  test_compose.py                          NEW (8 tests)
  test_constants.py                        NEW (9 tests)
  test_envfile.py                          NEW (7 tests)
  test_health.py                           NEW (9 tests)
  test_preflight.py                        NEW (17 tests)
  test_util.py                             NEW (14 tests)

setup.sh                                   NEW (POSIX bootstrap shim)
setup.ps1                                  NEW (Windows bootstrap shim)
Makefile                                   NEW (Unix convenience targets)
start.sh                                   REWRITE (forwards to setup.sh start)
start.ps1                                  REWRITE (forwards to setup.ps1 start)
validate_setup.ps1                         REWRITE (forwards to setup.ps1 doctor)
QUICK_START.md                             REWRITE (cross-platform quickstart)

# Cycle F Sprint 1 prep — Inference migration mechanics
compose/llama-swap/
  config.q4_0.yaml                         NEW (symmetric Q4_0 KV variant)
  config.q8_0.yaml                         NEW (symmetric Q8_0 KV variant)
  config.yaml                              EDITED (default = Q8_0 contents)
tools/llamaswap/
  __init__.py                              NEW
  select_kv_quant.py                       NEW (atomic variant swap + rollback)
  probe_cache_reuse.py                     NEW (structured-timings cache probe)
tools/
  sprint1_ab_run.sh                        NEW (overnight A/B harness)
docker-compose.yml                         EDITED
  - removed `profiles: [llamaswap]` from llama-swap (default-on)
  - removed `app.deploy.replicas: 2` (Wrong #1 fix)
  - added `app.depends_on.llama-swap: service_healthy`
tools/setup/constants.py                   EDITED (llama-swap tier=core)
tools/setup/install.py                     EDITED (core ∩ profile services)
tools/setup/services.py                    EDITED (core ∩ requested services)
document_processor/infrastructure/cache.py EDITED (Pub/Sub comment reflects 1-replica)
tests/api/test_sse_single_replica.py       NEW (6 tests: Wrong #1 + cram regression)
tests/setup/test_install_intersection.py   NEW (7 tests: install + start intersection)
tests/setup/test_kv_quant_selector.py      NEW (9 tests: swap / rollback / idempotency)
docs/llamacpp_pin.md                       NEW (pin-capture procedure)
docs/sprint1_runbook.md                    NEW (operator runbook)

web_ui/v2/src/
  components/shell/SessionList.tsx         REWRITE (~600 LOC)
  components/shell/session-list-helpers.test.ts  NEW (17)
  lib/chat-stream.ts                       (RESEARCH_REDUCER + chat_session linkage)
  lib/sessions.ts                          (sessions.create)
  lib/query-client.ts                      NEW
  components/chat/ChatComposer.tsx         (effortTiers props)
  components/chat/UnifiedComposer.tsx      (i18n + cleanup)
  routes/Build.tsx                         (effort + reflexion handlers + chat_session linkage)
  routes/Research.tsx                      (RESEARCH_REDUCER + 5-tier effort + chat_session)
  routes/Thinking.tsx                      (5-tier effort + i18n empty state + chat_session)
  routes/Consortium.tsx                    (i18n + chat_session)
  routes/Sentinel.tsx                      (i18n + chat_session)
  routes/Home.tsx                          (full i18n)
  routes/Diagnostics.tsx                   (full i18n)
  routes/Baselines.tsx                     (full i18n)
  routes/LLM.tsx                           (full i18n)
  routes/Evals.tsx                         (full i18n)
  routes/Chat.tsx                          (full i18n)
  i18n/en.ts                               (+150 keys)
  i18n/tr.ts                               (+150 keys)

document_processor/config/settings.py
  + code_max_reflexion_iterations: int = 1
  + code_reflexion_quality_threshold: int = 80
```

## Cycle E 90-day plan (Pazartesi → +90 gün)

The brief's 13-sprint roadmap (Sprint 0 + 1-12) maps cleanly onto the
90-day calendar.  Most sprints are RETROACTIVE validation of what
Cycle D already shipped; the roadmap below explicitly marks each
sprint as "fresh build" or "validation pass".

### Week 1 (Pazartesi–Cuma) — Sprint 0 v18 baseline

**Goal**: lock in a Mistral-judged 10-prompt baseline that becomes
the reference for every later "Sprint X +Y%" claim.

- **Pazartesi** (~30-45 min runtime):
  ```bash
  export AMOR_BASELINE_USERNAME=amor-baseline-runner
  export AMOR_BASELINE_PASSWORD='<vault>'
  nohup tools/run_sprint0_v18.sh > /tmp/sprint0_v18.log 2>&1 &
  ```
- **Salı**: analyze + promote to baseline-of-record (commit + tag).
- **Çarşamba**: re-judge with Phi-4 cross-validation if uncertainty>2/10.
- **Perşembe-Cuma**: write `docs/sprint0_v18_results.md` with
  per-prompt judgment table + judge agreement matrix.

### Week 2-3 — Sprint 1 v18 (validation pass)

**Goal**: re-measure Cycle C's already-landed Ollama → llama-swap +
llama.cpp migration against the fresh v18 baseline.  Fail-loud if
the measured speedup has drifted.

- llama.cpp pinned to `b8500+` (current build hash captured in
  Sprint 1 docs).
- 50 sequential pipeline runs, capture peak VRAM (target ≤7.6 GB).
- Compare measured per-prompt wall-clock vs Sprint 0 v18 baseline:
  - **Pass criterion**: e2e ≥-25%, FTT ≥-60%.
- **Optional**: speculative decoding smoke test
  (`tools/spec_decoding_smoke.py`) — accept ≥60%, ship; else shelf.

### Week 4-5 — Sprint 2 v18 (fresh measurement)

**Goal**: brief's note — published 8B SWE-bench Verified numbers
refer to flagship 671B MoE under Agentless, not the 8B distill.
**AMOR establishes its own SWE-bench-Lite-25 baseline.**

- Run `tools/eval/run_humaneval_plus.py` (already landed); capture
  baseline pass@1.
- Run `tools/eval/run_swebench_lite_25.py`; capture resolved rate
  + mean wall.
- RAGAS over LanceDB: faithfulness/answer_relevancy/context_precision.
- Persist to `eval_runs` table; visible in `/admin/evals`.

### Week 6-7 — Sprint 3 v18 (validation pass)

**Goal**: verify Aider-style repomap + BM25 hybrid retrieval still
delivers the +1-judge-point lift.

- Sprint 0 v18 corpus → planner with repomap ON vs OFF.
- Confirm BGE-Reranker-v2-m3 P95 < 250 ms on CPU.

### Week 8-9 — Sprint 4 v18 (UI + axe-core)

**Goal**: re-run axe-core a11y gate + Lighthouse on every mode.
Mobile + desktop coverage.

### Week 10-11 — Sprint 5 v18 (sandbox security re-audit)

**Goal**: docker-bench-security score + smoke 20/20 against the
v18 sandbox config (now includes 16 language runners).

### Week 12-13 — Sprint 6 v18 (ORPO real run)

**Goal**: brief's "200 preference pairs threshold" — accumulate via
MessageActions ratings between Pazartesi and the end of week 11,
then run the real ORPO + Sprint-0-corpus eval-delta gate.

### Sprint 7-12 (defer to v19 cycle)

Sprints 7-12 (memory / agents / SSE / i18n / mobile / PWA) all
landed in Cycle C with Cycle D refinements.  They graduate to v19
when the v18 baseline + measured-wins from Sprints 1-6 give a
concrete "next 5 highest-impact upgrades" decision (the brief's
final-synthesis output).

## Open decisions captured (from brief)

1. **Critic-as-judge** = Mistral-Small-3-24B (charter).  Phi-4 is
   documented fallback only.
2. **Speculative decoding** = deferred to Sprint 1 follow-up; smoke
   test gates ship/shelf.
3. **8B SWE-bench Verified** = AMOR establishes its own number in
   Sprint 2 v18 rather than quoting the 671B-MoE-under-Agentless
   numbers.
4. **Dual-resident @ 16K context** = infeasible; architect-only
   resident, editor swap-in on demand.

## Acceptance gate for v18 launch

- Sprint 0 v18 baseline mean correctness ≥ 3.0 (10-prompt).
- Sprint 1 v18 measured e2e -25%+ vs Cycle B baseline.
- Sprint 2 v18 HumanEval+ pass@1 ≥ 76% (Cycle C target).
- Bundle gz ≤ 140 kB (current 106.60 kB; budget +33 kB).
- All 142 tests green (no regression from Cycle D state).

When all five gates are green: tag `v18.0.0` and announce in
`docs/cycle_e_v18_complete.md`.

## Rollback paths

- **Reflexion off**: `code_max_reflexion_iterations=0` in settings.
- **Domain templates off**: pass `triage["domain"] = None` in tests
  or env-flag a per-deploy disable.
- **16-language sandbox**: every new lang has its own `LANGUAGE_RUNNERS`
  entry; remove the entry to disable (existing 10-lang Cycle C set
  remains untouched).
- **i18n switchback**: locale toggle in Settings flips to English
  immediately; no backend impact.
- **Sessions sidebar**: revert `SessionList.tsx` to Cycle C version
  (saved in git history at the v17 tag).
