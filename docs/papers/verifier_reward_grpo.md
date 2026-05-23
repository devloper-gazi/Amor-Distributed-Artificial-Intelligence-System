# Verifier-Reward GRPO + Compress-then-Recover: An Efficient-ML Workflow

**Status**: Draft / Preliminary results.  Sprint J.3 captures Cycle H.3
(GRPO) + Cycle J.1 (Saten TT compression) into a single methodology
report.  Target venue: ICML / NeurIPS workshop on Efficient ML 2027.

**Authors**: AMOR project (single-developer Local-AI Lab) — to be
filled at submission.

## TL;DR

We combine two complementary techniques to compress + recover a 7B
code-generation model on commodity hardware (RTX 4060 Laptop, 8 GB
VRAM):

  1. **GRPO with verifier-derived scalar rewards**.  Replacing
     human-rated preference pairs with reward signals computed from
     verifier outputs (Hypothesis property tests, mutmut, coverage,
     pylint, Phi-4 critic verdicts) closes the "rated-corpus famine"
     that single-user systems suffer.  At AMOR's ~5-10 sessions/day
     scale, ORPO would need 20-40 days to accumulate 200 pairs;
     verifier rewards make every session a labelled example
     immediately.
  2. **Saten Tensor-Train compression of FFN layers** (arXiv
     2505.14871) with sparse residual.  We target a 50% MLP
     parameter shrink with ≤3pp HumanEval+ loss, then RECOVER the
     lost capacity via 24h GRPO using the rewards from (1).

The novelty is the **closed loop**: the verifier reward signal that
trained the recovery LoRA is the same one we ALWAYS run as part of
the inference pipeline.  No specialised collection step — the
training data is a free byproduct of normal use.

## 1. Background

(Stub — to be filled at submission.)

- Local-AI code-gen state of the art (Qwen2.5-Coder, DeepSeek-R1,
  BitNet b1.58) on commodity laptop class.
- ORPO vs GRPO at small-corpus scale.
- TT decomposition for FFN compression: review of recent results.
- Verifier-reward training: RLHF without the H (humans).

## 2. Method

### 2.1 Verifier-reward signal

We aggregate four verifier categories into a single scalar reward
in [0, 1] per candidate completion:

| Verifier | Weight | Source |
|---|---|---|
| Hypothesis property tests | 0.40 | `code_intelligence.agents.PropertyTesterAgent` |
| Mutation testing (mutmut) | 0.20 | `code_intelligence.mutation_runner` |
| Branch coverage (pytest-cov) | 0.15 | `code_intelligence.coverage_reader` |
| Phi-4 critic verdict | 0.25 | `code_intelligence.agents.CriticAgent` |

(Weights are Plan-agent locked in `tools/training/verifier_rewards.py:WEIGHTS`.)

Each preference pair `(prompt, chosen, rejected)` is annotated with
`reward_chosen` and `reward_rejected` scalars; the GRPOTrainer
(TRL ≥0.18) consumes these directly.

### 2.2 Saten TT decomposition

(Stub — to be filled with the actual decomposition recipe applied
to Qwen2.5-Coder-7B's FFN weights, including the sparse-residual
fraction and target rank.)

### 2.3 Recovery protocol

Per `tools/training/saten_compression.py:compute_recovery_report`,
promotion requires BOTH:

  * `pre_pass_rate - post_pass_rate ≤ 3 pp` (loss bound), AND
  * `(post_grpo_pass_rate - post_pass_rate) / loss_pp ≥ 0.80`
    (recovery fraction)

The recovery training is 24h GRPO on the compressed model using
the verifier-reward annotated JSONL from §2.1.

## 3. Experimental setup

- **Hardware**: RTX 4060 Laptop (8 GB VRAM), 32 GB RAM, Windows 11
  + WSL2 + Docker.
- **Base model**: Qwen2.5-Coder-7B-Instruct Q4_K_M (4.68 GB on disk).
- **Eval set**: HumanEval+ 50-task subset (curated easy/medium).
- **Preference-pair corpus**: 30 days of AMOR session logs +
  synthetic pairs from `tools/training/synth_pair_generator.py`.
- **Trainer**: TRL 0.18 GRPOTrainer (`tools/training/orpo_qwen_coder.py
  --trainer-type grpo`).
- **Recovery wall-clock**: 24h on the 4060 (estimated; actual
  wall-clock TBD at submission time).

## 4. Results

(Stub — populated from `data/baselines/v18_launch_gate_*.json`,
`data/baselines/grpo_vs_orpo_latest.json`, and Saten-compression
recovery reports.)

- HumanEval+ pass@1 timeline: pre-compression → post-compression
  → post-GRPO recovery.
- ORPO baseline vs verifier-reward GRPO on property-test failure
  rate (Sprint-0 corpus, n=3 seeds, paired t-test).
- Per-verifier-category contribution ablation.

## 5. Discussion

- **Reproducibility**: the entire pipeline runs on a single
  laptop class machine.  See `tools/papers/reproduce_h3_j1.sh`
  (Sprint J.3 deliverable) for the one-script demonstration.
- **Generalisation**: the verifier-reward signal is task-specific
  (code generation with executable tests).  Applying the closed
  loop to non-executable domains (creative writing, math proofs)
  requires alternate verifier corpora.
- **Limitations**: AMOR's single-developer scale means we can't
  yet run the comparison against rated-corpus baselines that
  multi-developer organisations could collect.  The synthetic-pair
  generator (`synth_pair_generator.py`) substitutes for the missing
  data but is not equivalent.

## 6. Reproducibility kit

See `docs/papers/reproducibility_kit.md` for the one-script
demonstration that produces every number in this paper from a
clean checkout.  The kit includes:

  * Source GGUF download manifest (`tools/pull_models.py`).
  * Sprint-0 baseline runner with Mistral-Small-3 judge.
  * H.3 verifier-reward annotation cron.
  * J.1 Saten compression with `--simulate` for CI verification.
  * Recovery decision computation (`compute_recovery_report`).

## References

(Stub — to be filled at submission.)

  * Yang et al., 2024.  Kolmogorov-Arnold Transformer.
  * Saten et al., 2025.  Tensor-Train compression with sparse residual.
  * DeepSeek 2024.  GRPO for reasoning models.
  * BitNet b1.58 series.
  * Plan-agent acceptance gates (this work).

---

## Appendix A — Code pointers

| Concept | Code reference |
|---|---|
| Verifier-reward computation | `tools/training/verifier_rewards.py:compute_reward_breakdown` |
| GRPO trainer factory | `tools/training/orpo_qwen_coder.py:run` |
| Cron annotation step | `tools/training/orpo_weekly_cron.py:run` |
| Saten compression | `tools/training/saten_compression.py:compute_recovery_report` |
| KAT FFN kill-switch | `tools/training/kat_ffn_distill.py:kill_switch_decision` |
| Sprint-0 corpus | `tests/baselines/sprint0_prompts.json` |
| HumanEval+ runner | `tools/eval/humaneval_plus.py` |

## Appendix B — Plan-agent locked thresholds (audit trail)

Plan-agent reviewed + locked these thresholds on 2026-05-16; any
revision requires a separate audit entry.

```
GRPO_PROPERTY_FAILURE_REDUCTION_TARGET_PCT = 10.0
GRPO_MIN_SEEDS = 3
GRPO_MAX_P_VALUE = 0.05
SATEN_MAX_LOSS_PP = 3.0
SATEN_MIN_RECOVERY_FRACTION = 0.80
KAT_TARGET_PERPLEXITY_RATIO = 0.95
JEPA_TARGET_LOSS = 0.40
BITNET_AGREEMENT_TARGET_PCT = 85.0
BITNET_P95_LATENCY_MAX_MS = 6000.0
BITNET_MIN_SAMPLES = 200
```
