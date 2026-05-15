#!/usr/bin/env bash
# Cycle E — profile-driven judge launcher.  Wraps start_judge.sh,
# selecting the GGUF + container resources from
# tools/judge/judge_profiles.json based on the AMOR_SPRINT0_JUDGE env
# var (or --profile flag).
#
# Usage:
#   AMOR_SPRINT0_JUDGE=mistral tools/judge/select_and_start.sh
#   tools/judge/select_and_start.sh --profile phi4
#   tools/judge/select_and_start.sh --profile mistral_fast
#
# Falls back to "mistral" (the v18 default) when no profile is set.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
PROFILES_JSON="${REPO_ROOT}/tools/judge/judge_profiles.json"

# ─── Profile resolution ─────────────────────────────────────────────

PROFILE="${AMOR_SPRINT0_JUDGE:-}"
if [[ "${1:-}" == "--profile" ]] && [[ -n "${2:-}" ]]; then
    PROFILE="$2"
fi
if [[ -z "$PROFILE" ]]; then
    PROFILE="$(python3 -c "import json,sys;print(json.load(open(sys.argv[1]))['default'])" \
        "$PROFILES_JSON")"
fi

echo "[select_and_start] profile=$PROFILE"

# Pull profile fields via Python (avoids jq dependency).
read_field() {
    python3 -c "
import json, sys
profile = sys.argv[1]
field = sys.argv[2]
with open(sys.argv[3]) as f:
    cfg = json.load(f)
prof = cfg['profiles'].get(profile)
if prof is None:
    print('ERROR: unknown profile {!r}.  Known: {}'.format(profile, list(cfg['profiles'].keys())), file=sys.stderr)
    sys.exit(2)
val = prof.get(field, '')
print(val)
" "$PROFILE" "$1" "$PROFILES_JSON"
}

GGUF_NAME="$(read_field gguf_filename)"
MODEL_NAME="$(read_field model_name)"
LABEL="$(read_field label)"
MEM="$(read_field container_memory)"
CPUS="$(read_field container_cpus)"
CTX="$(read_field ctx_size)"
THREADS="$(read_field threads)"
HF_REPO="$(read_field huggingface_repo)"
HF_PATTERN="$(read_field huggingface_pattern)"

if [[ -z "$GGUF_NAME" ]]; then
    echo "[select_and_start] ERROR: profile $PROFILE missing gguf_filename" >&2
    exit 2
fi

echo "[select_and_start] $LABEL"
echo "[select_and_start]   gguf=$GGUF_NAME mem=$MEM cpus=$CPUS ctx=$CTX threads=$THREADS"

# ─── Pre-flight: GGUF in volume? ────────────────────────────────────

VOLUME="${AMOR_JUDGE_VOLUME:-amor_custom-models-data}"
# MSYS_NO_PATHCONV=1 prefix here only — Git Bash on Windows mangles
# the /v/judge/X path otherwise; we don't export it globally because
# that breaks the read_field python calls reading host paths.
if ! MSYS_NO_PATHCONV=1 docker run --rm -v "${VOLUME}:/v:ro" busybox \
        test -f "/v/judge/${GGUF_NAME}" >/dev/null 2>&1; then
    cat >&2 <<EOF
[select_and_start] ERROR: GGUF not found in volume ${VOLUME} at /judge/${GGUF_NAME}

To download into the volume (one-shot):
    docker exec amor-app-1 sh -c \\
      'mkdir -p /data/custom_models/judge && cd /data/custom_models/judge && \\
       hf download ${HF_REPO} --include "${HF_PATTERN}" --local-dir .'

(or use \`huggingface-cli\` if 'hf' alias is unavailable in the container)
EOF
    exit 2
fi

# ─── Hand off to start_judge.sh ─────────────────────────────────────

export AMOR_JUDGE_GGUF="$GGUF_NAME"
export AMOR_JUDGE_MEM="$MEM"
export AMOR_JUDGE_CPUS="$CPUS"
export AMOR_JUDGE_CTX="$CTX"
export AMOR_JUDGE_THREADS="$THREADS"
exec "$REPO_ROOT/tools/judge/start_judge.sh"
