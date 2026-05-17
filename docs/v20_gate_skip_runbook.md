# v20 Gate — 4 SKIP Operator Runbook

Status as of 2026-05-17: Cycle H + I (sub-sprints I.1 + I.2) +
J (scaffolds) + K (scaffolds) are CODE-COMPLETE.  The v20 launch
gate currently scores **2 PASS + 0 FAIL + 4 SKIP** (verdict
INCOMPLETE).  This runbook documents how the operator lifts each
SKIP to a measured PASS or FAIL.

## Summary

| # | Condition | Threshold | Current | Blocker |
|---|---|---|---|---|
| 1 | substrate_count | ≥3 | 3 ✅ | — |
| 2 | bitnet_agreement_pct | ≥85 % | SKIP | <200 samples + substitute model |
| 3 | bitnet_p95_latency_ms | ≤6000 ms | SKIP | <200 samples + substitute model |
| 4 | grpo_property_failure_reduction_pct | ≥10 % | SKIP | Real GRPO training run pending |
| 5 | lazygraphrag_ndcg_uplift_pct | ≥15 % | SKIP | Bench corpus + 100-q labels pending |
| 6 | vram_peak_gb | ≤7.2 GB | 6.17 GB ✅ | — |

## #2 + #3 — BitNet shadow window (200+ samples)

### Path A — real BitNet 2B-4T (preferred)

The Microsoft `bitnet-b1.58-2B-4T-gguf` repo ships only the
deprecated `TYPE_IQ4_NL_4_4` quantisation, which mainline llama.cpp
≥b9010 rejected.  When Microsoft's upstream `setup_env.py` codegen
patch lands for the 2B-4T tensor shape, swap the model in the
`amor-bitnet-shadow` llama-swap entry:

```yaml
# compose/llama-swap/config.yaml
"amor-bitnet-shadow":
  cmd: |
    llama-server -m /bitnet-models/ggml-model-i2_s.gguf  ...
```

Then enable the shadow planner + reduce the proxy timeout headroom
so 502s don't poison the sample stream:

```python
# document_processor/config/settings.py
code_bitnet_planner_enabled: bool = True
code_bitnet_planner_timeout_s: float = 12.0   # was 8 — 3B substitute needs more
code_bitnet_shadow_traffic_pct: float = 100.0
```

Restart `amor-app-2` and `amor-llama-swap`.  Run the synth load:

```bash
docker exec amor-app-2 python //app/tools/bitnet_shadow_synthetic_load.py \
    --samples 220 --reset --max-tokens 64 --timeout-s 12
```

Snapshot lands at `data/baselines/bitnet_shadow_latest.json`.  v20
gate reads it next run.

### Path B — substitute 3B model (current state)

The community `BoscoTheDog/bitnet_b1_58_3B_gguf` Q3_K_S model loads
on current llama.cpp but produces noisier output (chat template
tokens leak) and is ~2× slower than the official 2B-4T target.
The gate's locked thresholds (85% agreement, 6s p95) were
calibrated for the 2B-4T variant; the 3B substitute would
measure FAIL on both even with 200 samples.

Recommendation: keep this path SKIPPED until Path A lands.  Don't
ship a FAIL based on a known-substitute substrate.

## #4 — GRPO property-test failure reduction

### Requirements

* `requirements-training.txt` installed in a dedicated training venv
  (`trl>=0.18,<0.19`, `peft`, `unsloth`, `transformers`)
* GPU host with ≥6 GB free VRAM (4060 Laptop qualifies — pause serving)
* ≥200 preference pairs in `data/preference_pairs/coder.jsonl`
  (either real-rated via MessageActions or synthetic via
  `tools/training/synth_pair_generator.py`)

### Runbook

```bash
# 1. Pause serving (frees GPU VRAM)
docker compose stop llama-swap amor-app-2

# 2. Activate training venv
python -m venv .venv-training
source .venv-training/bin/activate
pip install -r requirements-training.txt

# 3. Generate / refresh preference pairs
python tools/training/synth_pair_generator.py \
    --role coder --pairs-per-prompt 5 --max-prompts 50 \
    --out data/preference_pairs/coder_synth.jsonl

# 4. Annotate with verifier rewards
python -c "from tools.training.verifier_rewards import annotate_jsonl_file; \
    annotate_jsonl_file('data/preference_pairs/coder_synth.jsonl', \
                        'data/preference_pairs/coder_synth.rewards.jsonl')"

# 5. Train both adapters (3 seeds each)
for seed in 42 137 271; do
    python tools/training/orpo_qwen_coder.py \
        --jsonl data/preference_pairs/coder_synth.rewards.jsonl \
        --out models/lora/coder-r16-orpo-seed${seed} \
        --trainer-type orpo --seed ${seed}
    python tools/training/orpo_qwen_coder.py \
        --jsonl data/preference_pairs/coder_synth.rewards.jsonl \
        --out models/lora/coder-r16-grpo-seed${seed} \
        --trainer-type grpo --seed ${seed}
done

# 6. Resume serving
docker compose up -d

# 7. Eval each adapter on the Sprint-0 + Aider polyglot 50 corpus
# (mount each adapter via llama-swap, run the eval harness,
#  capture per-prompt property-test failure rates)

# 8. Aggregate + paired t-test
python tools/training/aggregate_grpo_orpo_results.py \
    --out data/baselines/grpo_vs_orpo_latest.json

# 9. Re-run v20 gate — condition #4 lifts
python tools/run_v20_launch_gate.py
```

Step 8's aggregator is a thin script that:
1. Reads per-adapter eval output (one JSON per seed × per algo).
2. Computes property_failure_reduction_pct = (orpo_rate - grpo_rate) / orpo_rate.
3. Runs scipy.stats.ttest_rel on the per-seed reductions.
4. Writes `grpo_vs_orpo_latest.json` with `{property_failure_reduction_pct, seeds, p_value}`.

This script doesn't exist yet (Sprint H.3 +1 follow-on).  Operator
implements it once steps 5-7 produce data.

## #5 — LazyGraphRAG nDCG@10 uplift

### Requirements

* LanceDB corpus populated with content (no minimum size; AMOR's
  ~300 source files give a good multi-hop test bed)
* Ground-truth-labeled query bench at
  `tests/eval/lazy_graphrag_100_questions.json` (20 queries
  shipped Cycle H.2; grow toward 100 over Cycle I+)
* `settings.rag_graphrag_enabled` toggleable per-run (yes by default)

### Runbook

```bash
# 1. Index AMOR's repo (or alternate corpus)
docker exec -e PYTHONPATH=/app amor-app-2 python -u \
    /app/tools/index_amor_for_graphrag.py --root /app --chunk-lines 100

# 2. Build the LazyGraphRAG entity-graph index (one-shot, 20-40 min)
docker exec -e PYTHONPATH=/app amor-app-2 python -c "
import asyncio, sys
sys.path.insert(0, '/app')
async def main():
    from local_ai.vector_store.lancedb_store import LanceDBVectorStore
    store = LanceDBVectorStore(db_path='/data/vectors')
    stats = await store.build_lazy_graphrag_index()
    print(stats)
asyncio.run(main())
"

# 3. Run the bench (compares LanceDB-only vs LazyGraphRAG-on)
docker exec -e PYTHONPATH=/app amor-app-2 python \
    /app/tools/eval/lazy_graphrag_bench.py \
    --queries /app/tests/eval/lazy_graphrag_100_questions.json \
    --top-k 10 --threshold-pct 15.0 --json

# 4. Snapshot lands at data/baselines/lazygraphrag_bench_latest.json
# 5. Re-run v20 gate — condition #5 lifts
python tools/run_v20_launch_gate.py
```

The bench computes `ndcg_uplift_pct = (graphrag_ndcg - baseline_ndcg) / baseline_ndcg`.
Plan-agent locked: ≥15 % uplift is required to justify the
LazyGraphRAG layer's cost.

## #6 — VRAM envelope (already PASS)

Snapshot at `data/baselines/vram_envelope_latest.json` shows
6.17 GB peak with 3 substrates concurrently loadable.  Operator
who wants the canonical 14-day envelope runs:

```bash
docker exec amor-app-2 python //app/tools/aggregate_vram_envelope.py \
    --interval-s 30 --duration-s 1209600 --continuous \
    --from-exporter
```

(prometheus exporter must be live; expose on :9835 first)

## v20 verdict ladder

| Lifts completed | Expected verdict |
|---|---|
| None (current) | INCOMPLETE (2 PASS + 4 SKIP) |
| Only #5 LazyGraphRAG | INCOMPLETE (3 PASS + 3 SKIP) |
| Only #4 GRPO | INCOMPLETE (3 PASS + 3 SKIP) |
| #4 + #5 | INCOMPLETE (4 PASS + 2 SKIP) |
| #2 + #3 + #4 + #5 (full) | **PASS** (6 PASS + 0 SKIP) |

When `verdict == PASS`, tag `v20.0.0-rc1`.  Plan-agent locked: only
tag stable `v20.0.0` after a 14-day window with no regressions
post-rc1.
