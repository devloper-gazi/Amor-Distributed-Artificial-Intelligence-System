#!/usr/bin/env bash
# Wait for the Mistral-Small-3 GGUF download to complete, then start
# the judge llama-server.  Used by Sprint 0 Day 3 automation.
#
# Done = file size stable for 90 s (i.e. no growth across 3 polls).
# Timeout = 90 min total (override with first arg).

set -eo pipefail

# Git Bash on Windows mangles Linux container paths in `docker run`
# args (e.g. `/v/...` → `V:/...`).  Disable that conversion globally.
export MSYS_NO_PATHCONV=1

MAX_MIN=${1:-90}
POLL_S=30
STABLE_NEEDED=3   # 3 polls × 30 s = 90 s of no growth → done

GGUF_NAME="Mistral-Small-24B-Instruct-2501-Q4_K_M.gguf"

probe_size() {
  docker run --rm -v amor_custom-models-data:/v:ro busybox \
      stat -c '%s' "/v/judge/${GGUF_NAME}" 2>/dev/null || echo 0
}
probe_cache() {
  local out
  out=$(docker run --rm -v amor_custom-models-data:/v:ro busybox \
        du -s /v/judge/.cache 2>/dev/null | awk '{print $1}')
  echo "${out:-0}"
}

start_ts=$(date +%s)
prev_size=0
prev_cache=0
stable=0

echo "[$(date -Iseconds)] await GGUF (max ${MAX_MIN} min)..."

while true; do
  now=$(date +%s)
  elapsed_min=$(( (now - start_ts) / 60 ))
  if [ "${elapsed_min}" -ge "${MAX_MIN}" ]; then
    echo "[$(date -Iseconds)] TIMEOUT after ${MAX_MIN} min"
    exit 3
  fi

  size=$(probe_size)
  cache=$(probe_cache)
  size_gb=$(awk -v b="$size" 'BEGIN{printf "%.2f", b/1024/1024/1024}')
  cache_mb=$(awk -v k="$cache" 'BEGIN{printf "%.0f", k/1024}')

  if [ "$size" -gt 0 ] && [ "$size" = "$prev_size" ] && [ "$cache" = "$prev_cache" ]; then
    stable=$((stable+1))
    echo "[$(date -Iseconds)] stable poll ${stable}/${STABLE_NEEDED} (size=${size_gb} GB, cache=${cache_mb} MB)"
  else
    stable=0
    echo "[$(date -Iseconds)] downloading... size=${size_gb} GB cache=${cache_mb} MB elapsed=${elapsed_min}m"
  fi

  if [ "$stable" -ge "$STABLE_NEEDED" ] && [ "$size" -gt $((10 * 1024 * 1024 * 1024)) ]; then
    echo "[$(date -Iseconds)] download settled at ${size_gb} GB"
    break
  fi

  prev_size=$size
  prev_cache=$cache
  sleep "${POLL_S}"
done

# Sanity check.
if [ "$size" -lt $((13 * 1024 * 1024 * 1024)) ]; then
  echo "ERROR: GGUF too small (${size_gb} GB < 13 GB expected); aborting"
  exit 4
fi

echo "[$(date -Iseconds)] starting judge service..."
bash "$(dirname "$0")/start_judge.sh"

echo "[$(date -Iseconds)] waiting for judge /health to return 200..."
deadline=$((SECONDS + 180))   # cold-load 24B Q4 on CPU ~30-120s
while [ "$SECONDS" -lt "$deadline" ]; do
  body=$(curl -sS -m 3 http://127.0.0.1:9101/health 2>/dev/null || true)
  if echo "$body" | grep -q '"status"'; then
    echo "[$(date -Iseconds)] JUDGE_READY status=$(echo "$body" | head -c 80)"
    exit 0
  fi
  sleep 5
done

echo "ERROR: judge /health did not return 200 within 3 min"
docker logs amor-judge --tail 30 2>&1 || true
exit 5
