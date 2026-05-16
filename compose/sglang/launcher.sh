#!/usr/bin/env bash
# Cycle G G2 spike — launch SGLang with Qwen2.5-Coder-7B for the
# multi-tenant benchmark.  Mirrors llama-swap's port (9101 vs 9100)
# so the spike_benchmark.py tool can hit both with the same model
# argument.
#
# Usage (host):
#   bash compose/sglang/launcher.sh
#   # then in another shell:
#   python tools/inference/spike_benchmark.py \
#     --compare http://amor-llama-swap:9100,http://amor-sglang:9101 \
#     --kill-ratio 1.5 --out data/spike/sglang_vs_llama_swap.json
#
# Kill criterion: see docs/inference_engine_decision.md.

set -euo pipefail

MODEL_PATH="${MODEL_PATH:-/models/llamaswap/Qwen2.5-Coder-7B-Instruct-Q4_K_M.gguf}"
PORT="${PORT:-9101}"
HOST="${HOST:-0.0.0.0}"
MEM_FRACTION="${MEM_FRACTION:-0.70}"   # conservative on 8 GB
MAX_TOTAL_TOKENS="${MAX_TOTAL_TOKENS:-8192}"

if [[ ! -f "$MODEL_PATH" ]]; then
    echo "[sglang-launcher] FATAL: model not found at $MODEL_PATH" >&2
    exit 2
fi

echo "[sglang-launcher] starting SGLang on :$PORT"
echo "[sglang-launcher] model: $MODEL_PATH"
echo "[sglang-launcher] mem fraction: $MEM_FRACTION  max-total-tokens: $MAX_TOTAL_TOKENS"

# Note: SGLang's GGUF support is via the llama.cpp shim — performance
# parity not guaranteed.  The spike measures whether RadixAttention
# alone moves the needle on AMOR's shared-prefix pattern.
exec python -m sglang.launch_server \
    --model-path "$MODEL_PATH" \
    --port "$PORT" \
    --host "$HOST" \
    --mem-fraction-static "$MEM_FRACTION" \
    --max-total-tokens "$MAX_TOTAL_TOKENS" \
    --disable-cuda-graph \
    --enable-radix-cache
