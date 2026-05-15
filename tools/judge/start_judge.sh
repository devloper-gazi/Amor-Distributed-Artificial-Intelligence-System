#!/usr/bin/env bash
# Start a CPU-only Mistral-Small-3 judge llama-server for Sprint 0 baselines.
#
# The judge GGUF is expected at:
#   data/custom_models/judge/Mistral-Small-24B-Instruct-2501-Q4_K_M.gguf
#
# Pre-flight: the GGUF lands here via
#   hf download bartowski/Mistral-Small-24B-Instruct-2501-GGUF \
#       --include "*Q4_K_M*" --local-dir data/custom_models/judge
#
# Service is bound to 127.0.0.1:9101 (host-only); not part of
# docker-compose.yml on purpose — it's an opt-in dev tool, not a
# production-stack member.  Tear down with `tools/judge/stop_judge.sh`.

set -euo pipefail

# Git Bash on Windows mangles Linux container paths in `docker run`
# args.  Disable globally so volume mounts and -m model paths land
# inside the container correctly.
export MSYS_NO_PATHCONV=1

REPO_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
# AMOR's GGUFs live in a docker NAMED volume (amor_custom-models-data),
# not a host bind-mount, so the judge container references the volume
# directly.  Inside the volume, judge GGUFs go under /judge/.
VOLUME="${AMOR_JUDGE_VOLUME:-amor_custom-models-data}"
GGUF_NAME="${AMOR_JUDGE_GGUF:-Mistral-Small-24B-Instruct-2501-Q4_K_M.gguf}"
IMAGE="${AMOR_JUDGE_IMAGE:-ghcr.io/ggml-org/llama.cpp:server}"
HOST_PORT="${AMOR_JUDGE_PORT:-9101}"
CTX_SIZE="${AMOR_JUDGE_CTX:-4096}"
THREADS="${AMOR_JUDGE_THREADS:-8}"

# Verify the GGUF exists in the named volume.  We can't `ls` the
# volume from the host directly; use a one-shot busybox container.
if ! docker run --rm -v "${VOLUME}:/v:ro" busybox \
        test -f "/v/judge/${GGUF_NAME}" >/dev/null 2>&1; then
    echo "ERROR: GGUF not found in volume ${VOLUME} at /judge/${GGUF_NAME}" >&2
    echo "Hint: docker exec amor-app-1 sh -c \\" >&2
    echo "    'cd /data/custom_models/judge && hf download \\" >&2
    echo "      bartowski/Mistral-Small-24B-Instruct-2501-GGUF \\" >&2
    echo "      --include \"*Q4_K_M*\" --local-dir .'" >&2
    exit 2
fi

# Stop any previous instance.
docker rm -f amor-judge >/dev/null 2>&1 || true

docker run -d --rm \
    --name amor-judge \
    -v "${VOLUME}:/data/custom_models:ro" \
    -p "127.0.0.1:${HOST_PORT}:8080" \
    --memory="${AMOR_JUDGE_MEM:-16g}" \
    --cpus="${AMOR_JUDGE_CPUS:-8}" \
    "${IMAGE}" \
    -m "/data/custom_models/judge/${GGUF_NAME}" \
    --host 0.0.0.0 --port 8080 \
    --ctx-size "${CTX_SIZE}" \
    --threads "${THREADS}" \
    --batch-size 256 \
    --no-webui \
    --metrics

echo "amor-judge starting on http://127.0.0.1:${HOST_PORT}"
echo "logs: docker logs -f amor-judge"
echo "health: curl -s http://127.0.0.1:${HOST_PORT}/health"
