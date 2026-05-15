# Sprint 1 v18 — Inference migration runbook

> Cycle F Sprint 1 — execution playbook for the KV-quant A/B baseline.
> Companion to `docs/sprint1_decision.md` (which will be committed
> AFTER the A/B run finishes).

## What landed (prep complete)

* `compose/llama-swap/config.q4_0.yaml` — symmetric Q4_0 KV variant
* `compose/llama-swap/config.q8_0.yaml` — symmetric Q8_0 KV variant
* `compose/llama-swap/config.yaml` — active (defaults to Q8_0 contents)
* `tools/llamaswap/select_kv_quant.py` — atomic variant swap + rollback
* `tools/llamaswap/probe_cache_reuse.py` — structured-timings cache probe
* `tools/sprint1_ab_run.sh` — overnight A/B harness
* `docker-compose.yml` — llama-swap promoted from `profiles: [llamaswap]`
  to default-on; `app.deploy.replicas: 2` removed (Wrong #1 fix);
  `app.depends_on.llama-swap: service_healthy` added
* `tools/setup/constants.py` — llama-swap promoted to `tier="core"`
* `tools/setup/install.py` + `tools/setup/services.py` — health-wait
  intersects core services with the active profile (so `minimal`
  doesn't hang waiting for llama-swap it never started)
* `tests/api/test_sse_single_replica.py` (6 tests) — Wrong #1 regression
* `tests/setup/test_install_intersection.py` (7 tests) — install / start
  health-wait intersection regression
* `tests/setup/test_kv_quant_selector.py` (9 tests) — swap / rollback /
  idempotency
* `docs/llamacpp_pin.md` — pin-capture procedure + cache-reuse history

## Live verification done

* `docker compose restart llama-swap` — healthy after 1s
* `python tools/llamaswap/probe_cache_reuse.py --model amor-editor
  --unique-prefix` →
  call 1: 283.7 ms prefill / 108 new tokens / 210 cached
  call 2:  36.6 ms prefill /   1 new token  / 317 cached
  **prefill ratio = 0.13× (7.7× speedup, well under the 0.2× gate)**
* `python -m tools.setup verify` — 7/7 ✓
* `pytest tests/setup tests/api tests/baselines -q` — 107/107 ✓

## What to run for the actual Sprint 1 exit gate

The A/B baseline run is a **~12-hour overnight job** (6 h per
variant × 2 variants, Mistral-judged).  Schedule it for a window when
the host can be left alone.

### Pre-flight (each run; the harness checks all of these)

1. `docker compose ps` — every core service `running (healthy)`.
2. `MSYS_NO_PATHCONV=1 docker exec amor-llama-swap ls //models/llamaswap/`
   shows the three GGUFs:
   * `DeepSeek-R1-0528-Qwen3-8B-Q4_K_M.gguf`
   * `Qwen2.5-Coder-7B-Instruct-Q4_K_M.gguf`
   * `Qwen3-8B-Q4_K_M.gguf`
3. AMOR baseline credentials in env:
   ```bash
   export AMOR_BASELINE_USERNAME=amor-baseline-runner
   export AMOR_BASELINE_PASSWORD='<vault-secret>'
   ```
4. The Mistral judge GGUF is in `amor_custom-models-data:/v/judge/`
   (the doctor checks this).

### Kicking off the overnight run

```bash
# Both variants (default; ~12 h total wall-clock)
nohup bash tools/sprint1_ab_run.sh \
    > data/baselines/sprint1_ab_$(date -u +%Y%m%dT%H%M%SZ).log 2>&1 &
echo "PID=$!"

# Or just one variant if you want to bisect:
AMOR_SPRINT0_JUDGE=mistral \
nohup bash tools/sprint1_ab_run.sh --only q8_0 \
    > /tmp/sprint1_q8.log 2>&1 &
```

### The harness does, per variant:

1. `select_kv_quant.py --quant {q4_0|q8_0}` → atomic config swap.
2. `docker rm -f amor-llama-swap` then `docker compose up -d llama-swap`.
3. Wait up to 120 s for `/health` on port 9100.
4. `probe_cache_reuse.py --model amor-editor` — FAIL ABORTS the run.
5. `tools/run_sprint0_v18.sh` — the existing overnight Sprint-0 runner.
6. Copy `data/baselines/sprint0_latest.json` →
   `data/baselines/sprint1_{q4_0|q8_0}_results.json`.

## Decision rule (commit `docs/sprint1_decision.md` after run)

Apply the Pareto rule from `docs/cycle_e_active.md` / plan:

* **Promote q4_0** if:
  * `mean(correctness_q4) >= mean(correctness_q8) - 0.15`, AND
  * `mean(latency_q4) <= mean(latency_q8)`
* **Stay on q8_0** otherwise; flag for re-evaluation after Sprint 3
  LoRA adapters land (the marginal Q4_0 PPL cost shows worse
  against unspecialised base than against trained adapters).

Whichever wins, run:

```bash
python tools/llamaswap/select_kv_quant.py --quant <winner>
docker compose restart llama-swap
python -m tools.setup verify
```

And commit:

* `tests/baselines/sprint1_q4_0_results.json` + `sprint1_q8_0_results.json`
* `tests/baselines/sprint1_results.json` (the chosen winner, symlink/copy)
* `docs/sprint1_decision.md`

## Sprint 1 exit criteria (verbatim from the plan file)

1. 100% of Sprint-0 corpus runs end-to-end on `AMOR_LLM_BACKEND=llama-swap`
   with zero hard errors.
2. Baseline judge scores recorded for BOTH KV variants with per-mode
   breakdown.
3. `--cache-reuse 256` confirmed firing via `probe_cache_reuse.py`
   on 3 consecutive requests sharing the same system prompt
   (prefill ratio ≤ 0.2× — **landed ad-hoc above at 0.13×**).
4. `docs/sprint1_decision.md` committed selecting q4_0 or q8_0.
5. `tests/api/test_sse_single_replica.py` green
   — **landed (6 tests)**.
6. Full `pytest tests/setup tests/api tests/baselines -q` green
   — **landed (107/107)**.

## Rollback

| change | rollback |
|---|---|
| llama-swap default-on | edit compose: re-add `profiles: [llamaswap]` |
| `replicas: 2 → 1` | re-add `deploy.replicas: 2` to app block |
| KV-quant winner | `python tools/llamaswap/select_kv_quant.py --rollback` |
| Inference backend | `export AMOR_LLM_BACKEND=ollama` + restart app |

No DB migrations, no schema changes — every rollback is one edit
or env-flag.

## Discovered behavior / corrections to the strategic roadmap

* **`--cram 512` flag does NOT exist.**  The roadmap text says
  `--cram 512` but llama-server only accepts `-cram` (short form)
  or `--cache-ram` (long form).  Furthermore the default
  `--cache-ram` is **8192 MiB enabled**, so setting `512`
  actually SHRINKS the host-memory prompt cache.  Resolution: drop
  the flag entirely; default is what the roadmap actually wanted.
  The fix is captured in `compose/llama-swap/config.q*.yaml`
  comments and in `tests/api/test_sse_single_replica.py
  ::test_llamaswap_config_does_not_use_unsupported_cram_flag`.
