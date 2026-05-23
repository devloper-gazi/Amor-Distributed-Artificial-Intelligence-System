# Reproducibility Kit — Verifier-Reward GRPO + Compress-then-Recover

Plan-agent locked: every number in `verifier_reward_grpo.md` must
be reproducible from a clean checkout by following the steps below.
This file lists the **commands** the operator runs; the produced
artefacts land at known paths under `data/baselines/`.

## Prerequisites

- Linux (or Windows + WSL2) host with NVIDIA GPU (RTX 4060 8 GB
  validated; larger GPUs work).
- 32 GB host RAM.
- 120 GB free disk for Docker images + GGUFs + Saten artefacts.
- Docker Desktop ≥4.30 (CVE-2025-31133 remediation).
- Python 3.11 (the runtime container's pin).

## Quick run

The one-script entrypoint (Sprint J.3 deliverable):

    bash tools/papers/reproduce_h3_j1.sh

This invokes the four phases below in order.  Expect ~36h total
wall-clock when actual training runs; ~5 min when every step uses
`--simulate` / `--dry-run` (CI verification mode).

## Phase 1 — Pull the GGUFs

    python tools/pull_models.py

Pulls Qwen2.5-Coder-7B Q4_K_M + Mistral-Small-3-24B (judge) into
the `custom-models-data` named volume.  Cached after first run.

## Phase 2 — Sprint-0 baseline

    bash tools/run_sprint0_v18.sh

~90 min wall.  Produces `data/baselines/sprint0_<utc>.jsonl` +
`sprint0_latest.json` with per-prompt judge scores.

## Phase 3 — H.3 GRPO with verifier rewards

### 3a. Accumulate preference pairs

Either from real session logs (when ≥200 rated pairs accumulate
in Postgres) OR via the synthetic generator:

    python tools/training/synth_pair_generator.py \
        --role coder --pairs-per-prompt 5 \
        --out data/preference_pairs/coder_synth.jsonl

### 3b. Annotate with verifier rewards

Invoke `tools/training/verifier_rewards.py:annotate_jsonl_file`:

    python -c "from tools.training.verifier_rewards import annotate_jsonl_file; \
        annotate_jsonl_file('data/preference_pairs/coder_synth.jsonl', \
                            'data/preference_pairs/coder_synth.rewards.jsonl')"

### 3c. Train

    pip install -r requirements-training.txt    # heavy deps, ~3 GB
    python tools/training/orpo_qwen_coder.py \
        --jsonl data/preference_pairs/coder_synth.rewards.jsonl \
        --out models/lora/coder-r16-grpo \
        --trainer-type grpo

~60-90 min on RTX 4060.  Output: PEFT adapter at the `--out` dir
+ converted GGUF.

### 3d. Eval comparison

    python tools/run_v19_launch_gate.py --shallow

Reads the just-produced metrics and compares to the ORPO baseline
under `data/baselines/grpo_vs_orpo_latest.json`.

## Phase 4 — J.1 Saten TT compression + recovery

### 4a. Compress (operator: PAUSE serving first)

    docker compose stop llama-swap amor-app-2
    python tools/training/saten_compression.py \
        --model qwen2.5-coder-7b-q4_k_m \
        --target-rank 0.5 \
        --sparse-fraction 0.05 \
        --out models/lora/qwen-coder-saten-r0.5.peft

~4-8h wall (CPU-offload friendly with 32 GB RAM).

### 4b. Resume serving + measure pre/post

    docker compose up -d
    python tools/eval/humaneval_plus.py --model qwen2.5-coder-7b
    # ← record baseline pass@1, then load adapter:
    python tools/eval/humaneval_plus.py --model qwen2.5-coder-7b \
        --lora-adapter models/lora/qwen-coder-saten-r0.5.peft

### 4c. 24h GRPO recovery

    python tools/training/orpo_qwen_coder.py \
        --jsonl data/preference_pairs/coder.rewards.jsonl \
        --out models/lora/qwen-coder-saten-r0.5-recovered.peft \
        --trainer-type grpo \
        --epochs 3

### 4d. Acceptance decision

    python tools/training/saten_compression.py \
        --simulate \
        --pre-pass-rate <X> \
        --post-pass-rate <Y> \
        --post-grpo-recovery-pp <Z> \
        --out-report data/baselines/saten_recovery_latest.json

Promotion-ready when exit code 0.

## Phase 5 — Final v20 gate scorecard

    python tools/run_v20_launch_gate.py

Reads ALL accumulated baselines + writes
`data/baselines/v20_launch_gate_<utc>.json`.  This is the canonical
status snapshot we cite in the paper §4 Results.

## CI smoke mode (no GPU, no real training)

Every script that produces a measurement has a `--simulate` or
`--dry-run` mode that exercises the orchestration + decision logic
without launching the heavy ML pipeline.  Use these to verify the
reproducibility kit's plumbing without a GPU:

    python tools/training/saten_compression.py --simulate \
        --pre-pass-rate 78.0 --post-pass-rate 76.0 \
        --post-grpo-recovery-pp 1.6
    python tools/training/kat_ffn_distill.py --simulate \
        --observed-ppl 1.04 --baseline-ppl 1.0
    python tools/training/jepa_plan_predictor.py --simulate \
        --train-loss 0.30 --val-loss 0.38

Combined CI run: ~5 min.  Produces a synthetic but well-shaped
scorecard.

## Audit trail

Every produced artefact is timestamped (`computed_at_utc` field)
and persisted under `data/baselines/`.  Plan-agent acceptance
thresholds are baked into the scripts' defaults; revisiting them
requires a separate audit entry in the plan dosyası.
