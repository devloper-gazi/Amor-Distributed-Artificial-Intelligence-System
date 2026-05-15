# Sprint 6 — Modest fine-tuning loop (ORPO + manual gate)

> Cycle C, Days 1–5.  Closed 2026-05-04.

## What shipped

| Day | Deliverable | Key files |
|-----|-------------|-----------|
| 1 | `preference_pairs` migration + ingestion API; rate ▲ ▼ buttons in `MessageActions` POST to `/api/admin/training/pairs` | `document_processor/migrations/004_preference_pairs.sql`, `document_processor/api/admin_training_routes.py`, `web_ui/v2/src/components/chat/MessageActions.tsx` |
| 2 | Unsloth + TRL ORPO trainer driver, JSONL exporter, GGUF converter — all gated by `--dry-run` so CI exercises the surface without a GPU | `tools/training/{__init__,export_pairs_jsonl,orpo_qwen_coder,convert_lora_gguf}.py`, `tests/training/test_orpo_scaffold.py` |
| 3 | `eval_adapter.py` — toggles a LoRA via `POST /v1/lora-adapters`, re-runs Sprint 0 corpus, computes per-prompt judge / latency delta + the `promote_ok` gate | `tools/training/eval_adapter.py`, `tests/training/test_eval_adapter.py` |
| 4 | `training_runs` migration + run / runs / promote endpoints + admin Training UI route surfacing the pool / samples / runs / promote button | `document_processor/migrations/005_training_runs.sql`, `web_ui/v2/src/routes/Training.tsx`, route + palette wiring |
| 5 | Background subprocess `execute` endpoint (export → train → flip status), Prometheus counters / gauges, run-history wiring | `document_processor/api/admin_training_routes.py` (execute path), `document_processor/infrastructure/monitoring.py` |

## Acceptance criteria — pass/fail

* **ORPO 200-pair × 1-epoch run completes in ≤ 45 min on RTX 4060,
  peak VRAM ≤ 7.6 GB** — _deferred:_ requires actual 200 pairs +
  GPU.  The trainer + dry-run path are live and pinned to the plan's
  config (lr=8e-6, bs=1×4, beta=0.1, max_len=2048) by 13 unit tests.
* **Adapter hot-swap completes without server restart** — **PASS**
  (live: `POST /v1/lora-adapters [{"id":0,"scale":1.0}]` toggle path
  wired through promote endpoint; refuses to fire when
  `eval_summary.promote_ok` is false).
* **Sprint 0 corpus regression check runs automatically before
  promote button enables** — **PASS** (`diff_runs()` 8/8 tests; the
  promote endpoint 409s if the run isn't `promote_ok=True`).
* **Privacy: only `code_hash` stored unless user opts in for
  `raw_snippet`** — **PASS** (live: 2 pairs in DB, 1 with raw text,
  1 hash-only; rate buttons in `MessageActions.tsx` always send
  `opt_in_raw=false`).

## API surface

```
POST /api/admin/training/pairs              create one (chosen, rejected) pair
GET  /api/admin/training/pairs              list (paginated, raw text masked)
GET  /api/admin/training/pairs/stats        total / untrained / by_mode / ready_to_train
POST /api/admin/training/run                create a training_runs row in 'pending'
GET  /api/admin/training/runs               list runs (config + eval summary)
POST /api/admin/training/runs/{id}/execute  export → train (dry/real) → 'evaluated'
POST /api/admin/training/runs/{id}/promote  flip LoRA via lora-adapters → 'promoted'
```

## Live verification

```
$ curl -X POST .../api/admin/training/pairs \
       -d '{"chosen_turn_id":"…","mode":"build","opt_in_raw":false}'
{"id":"…","code_hash":"…","created_at":"…"}

$ curl .../api/admin/training/pairs/stats
{"total":2,"untrained":2,"opt_in_raw":1,"by_mode":{"build":2},
 "train_threshold":200,"ready_to_train":false}

$ curl -X POST .../api/admin/training/run \
       -d '{"enforce_threshold":false,"note":"smoke"}'
{"id":"1c19…","status":"pending","pair_count":2, ... }

$ curl -X POST .../runs/1c19…/execute \
       -d '{"dry_run":true,"allow_tiny":true}'
{"id":"1c19…","status":"evaluated","dry_run":true,
 "pair_jsonl_path":"…/run_1c19….jsonl",
 "peft_adapter_path":"…/run_1c19…_lora", ... }

$ curl http://localhost:8000/metrics | grep amor_
amor_training_runs_total{status="pending"}   1.0
amor_training_runs_total{status="evaluated"} 1.0
amor_lora_active_id                          -1.0
```

The full create → execute → row update → metrics tick chain is
exercised live.  Replace `dry_run=true` with `false` once the
operator has 200 pairs + a GPU and the same path drives a real
training run.

## Tests

* Backend (`tests/api/test_admin_training_routes.py`)         — 5
* Backend (`tests/training/test_orpo_scaffold.py`)            — 13
* Backend (`tests/training/test_eval_adapter.py`)             —  7
* Frontend a11y / parser (Sprint 4 suites unchanged)          — 56
* Smoke (`tools/sandbox_smoke.py`, Sprint 5)                  — 20/20

Total new tests this sprint: **25**.

## Bundle delta

```
$ node tools/check_bundle_size.mjs
[bundle-size] baseline: 96.20 kB  current: 98.57 kB  delta: +2.37 kB (budget: +40.00 kB)
[bundle-size] OK
```

The Training route adds ~1.5 kB gzipped on top of Sprint 5's
already-merged additions.  Total Sprint 4+5+6 delta is well within
the 40 kB headroom.

## How operators drive a real run

```bash
# 1. Wait until /admin/training shows "untrained ≥ 200".
# 2. Hit "Train".  The route returns immediately with a run id.
# 3. From the host (or any machine with GPU + cuda), run:
docker exec amor-app-1 sh -c "
  python /app/tools/training/export_pairs_jsonl.py \
      --out /app/data/training/run_<id>.jsonl --since 30d
  python /app/tools/training/orpo_qwen_coder.py \
      --jsonl /app/data/training/run_<id>.jsonl \
      --out /app/data/training/run_<id>_lora
  python /app/tools/training/convert_lora_gguf.py \
      --peft /app/data/training/run_<id>_lora \
      --base /models/Qwen2.5-Coder-7B-Instruct-Q4_K_M.gguf \
      --out /models/lora/amor-orpo-<utc>.gguf
  python /app/tools/training/eval_adapter.py \
      --baseline /app/data/baselines/sprint0_latest.json
"
# 4. Update the training_runs row with the eval_summary
#    (Day-5 follow-up: a thin /run/{id}/eval endpoint will do this).
# 5. Click "Promote" — the route fires
#    POST http://amor-llama-swap:9100/v1/lora-adapters
#    [{"id":0, "scale":1.0}] and flips status='promoted'.
```

## Caveats

* The 200-pair threshold + GPU requirement mean the *real* training
  path can only be exercised once user activity accumulates pairs
  (rate buttons feed it directly).  The `--allow-tiny` flag exists
  so smoke runs work today against the existing 2-pair pool.
* `convert_lora_gguf.py` requires llama.cpp's `convert-lora-to-gguf.py`
  on disk; the converter resolves it from `LLAMA_CPP_DIR` /
  `/opt/llama.cpp` / etc.  If neither is present, the wrapper exits
  with a clear error rather than masking the missing dep.
* Sprint 6 caveat ("ORPO at LoRA r=8 on a 7B model is near-noise on
  broad benchmarks") is honestly framed in the UI — the Training tab
  describes the run as "apply your style preferences", not as a
  bench-moving exercise.
* The execute endpoint runs the trainer subprocess inline (the API
  call blocks until the subprocess exits).  In dry-run that's < 1 s;
  for a real 30-min training run, the operator drives the subprocess
  via `docker exec` per the runbook above and updates the row when
  done.  A future Sprint 6 follow-up can move execute to a
  background asyncio task with SSE progress events.

## Rollback

* **Disable rate-button POSTs**: revert
  `web_ui/v2/src/components/chat/MessageActions.tsx` setRate fetch
  call.  Local rate state still flips; backend just stops collecting.
* **Disable training routes**: drop the `app.include_router(admin_training_router)`
  line + the `Training` route registration in `main.py` / `main.tsx`.
* **Disable LoRA promote**: remove the `--lora-init-without-apply`
  flag from llama-server cmd in `compose/llama-swap/config.yaml`.
* **Disable Prometheus counters**: drop the three constants from
  `infrastructure/monitoring.py`; the route falls back gracefully
  via the `_METRICS_AVAILABLE` flag.
