#!/usr/bin/env bash
# Cycle H.1 — bitnet-server entrypoint.
#
# 1. Ensure the GGUF exists at $MODEL_DIR/$MODEL_FILENAME; pull from
#    Hugging Face when missing.
# 2. Exec llama-server with sensible CPU-only flags for the b1.58 2B4T
#    model (Plan-agent locked: ctx 4096 keeps RAM under 2 GB, threads
#    8 saturates a typical laptop CPU, no GPU offload).
set -euo pipefail

MODEL_PATH="${MODEL_DIR}/${MODEL_FILENAME}"

# Lazy pull — first boot only.  Operator can pre-seed by writing the
# GGUF into the bind-mounted models/bitnet/ directory before bringing
# the service up.
if [[ ! -f "$MODEL_PATH" ]]; then
    echo "[bitnet] model missing at $MODEL_PATH — pulling $MODEL_REPO ..."
    mkdir -p "$MODEL_DIR"
    python -c "
from huggingface_hub import hf_hub_download
import os, shutil
out = hf_hub_download(
    repo_id=os.environ['MODEL_REPO'],
    filename=os.environ['MODEL_FILENAME'],
    local_dir=os.environ['MODEL_DIR'],
    local_dir_use_symlinks=False,
)
print(f'[bitnet] downloaded: {out}')
"
fi

echo "[bitnet] starting llama-server (ctx=$CTX_SIZE threads=$THREADS port=$PORT)"
exec /usr/local/bin/llama-server \
    -m "$MODEL_PATH" \
    --host "$HOST" \
    --port "$PORT" \
    --ctx-size "$CTX_SIZE" \
    --threads "$THREADS" \
    --metrics
