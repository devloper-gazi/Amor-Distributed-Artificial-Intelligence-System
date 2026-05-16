# Cycle G G2 — inference engine decision (SGLang vs llama-swap)

> **Status:** Spike infrastructure landed (`compose/sglang/`,
> `tools/inference/spike_benchmark.py`).  Live benchmark NOT executed
> in v18.1.3 because: (a) SGLang's 8 GB VRAM headroom is unproven
> on the reference host (Plan-agent HIGH-risk flag); (b) running the
> shootout against the live llama-swap would interrupt the v18.1
> launch gate measurements.  Operator-led benchmark when bandwidth
> permits; this doc captures the decision template.

## Decision

**Default: keep llama-swap.**  Migrate ONLY if the spike benchmark
shows SGLang p95 throughput ≥ 1.5× llama-swap's on the AMOR
4-concurrent-build-session pattern.

## Kill criterion (Plan-agent locked)

```
challenger_throughput_tokens_per_s / incumbent_throughput_tokens_per_s
    ≥ 1.5  → MIGRATE (Cycle H)
    <  1.5 → KEEP llama-swap, ABANDON spike branch
```

The benchmark uses a shared 1000-token prefix across 4 concurrent
identical requests — mirrors AMOR's worst case (4 build sessions
hitting the editor model with the same system + plan prefix).  Cache
reuse wins show up as 2nd-4th requests being measurably faster than
the 1st.

## How to run the benchmark

```bash
# 1. Start SGLang in a sibling container (host shell)
bash compose/sglang/launcher.sh &

# 2. Wait for it to come up
until curl -fsS http://localhost:9101/v1/models >/dev/null 2>&1; do
    sleep 2
done

# 3. Side-by-side bench
python tools/inference/spike_benchmark.py \
    --compare http://amor-llama-swap:9100,http://amor-sglang:9101 \
    --kill-ratio 1.5 \
    --rounds 3 --concurrency 4 --max-tokens 64 \
    --out data/spike/sglang_vs_llama_swap.json

# 4. Read the verdict
jq .verdict data/spike/sglang_vs_llama_swap.json
```

## Risks (Plan-agent surface)

| risk | severity | mitigation |
|---|---|---|
| SGLang OOMs at concurrency >1 on 8 GB VRAM | HIGH | `--mem-fraction-static 0.70` + `--max-total-tokens 8192` keeps the paged KV under 1 GB; if it still OOMs, spike fails the kill criterion ⇒ keep llama-swap |
| GGUF support via llama.cpp shim is slower than native | HIGH | Test with native SGLang weights if GGUF can't compete; if even native loses by ≥1.5× we keep llama-swap |
| Spike interrupts the live llama-swap stack | MED | Run the bench OFF-hours; isolate via `--scale llama-swap=0` if needed |
| Result is close to 1.5× — judgment call | MED | Re-run 3 rounds with different prompt sizes; ratio must be consistently ≥1.5×, not a single fluke |

## Re-evaluation triggers

* **Hardware step-up to ≥ 16 GB VRAM** — paged KV becomes feasible
  without the headroom dance; re-run the bench unconstrained.
* **SGLang ships native GGUF support** — the llama.cpp shim
  performance gap closes; re-evaluate.
* **AMOR concurrency target rises >5 sessions** — RadixAttention's
  multi-session advantage grows; the 1.5× kill ratio may be
  conservative.

## Decision log

| Date | Verdict | Throughput (incumbent / challenger) | Notes |
|---|---|---|---|
| 2026-05-16 | **KEEP (deferred)** | not yet measured | Spike infrastructure landed; live bench is operator GPU work, gated on a free overnight window. |
