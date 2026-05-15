#!/usr/bin/env bash
# Cycle F Sprint 1 — KV-quant A/B baseline harness.
#
# Runs the Sprint-0 v18 baseline TWICE — once with -ctk q8_0 -ctv q8_0
# and once with -ctk q4_0 -ctv q4_0 — and persists both scorecards
# so the Pareto-rule decision in docs/sprint1_decision.md is data-driven.
#
# Total wall-clock: ~12 hours (6 h per variant, Mistral judge on CPU).
# Designed to be left running unattended overnight.
#
# Usage:
#     export AMOR_BASELINE_USERNAME=amor-baseline-runner
#     export AMOR_BASELINE_PASSWORD='<vault-secret>'
#     bash tools/sprint1_ab_run.sh                  # both variants
#     bash tools/sprint1_ab_run.sh --only q8_0     # one variant only
#     bash tools/sprint1_ab_run.sh --only q4_0
#
# Each pass:
#   1. select_kv_quant.py points config.yaml at the variant
#   2. compose recreates llama-swap with the new config
#   3. probe_cache_reuse.py asserts cache-reuse is active
#   4. tools/run_sprint0_v18.sh runs the Mistral-judged 10-prompt corpus
#   5. result tagged + persisted as data/baselines/sprint1_<variant>_results.json
#
# Exit codes:
#   0  both passes succeeded
#   1  one or more passes failed
#   2  fatal init (compose missing, env missing, GGUF missing)

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")" && cd .. && pwd)"
cd "$REPO_ROOT"

# ─── Arg parsing ────────────────────────────────────────────────────

ONLY=""
while [[ $# -gt 0 ]]; do
    case "$1" in
        --only)
            ONLY="${2:-}"
            shift 2
            ;;
        -h|--help)
            sed -n '2,30p' "$0"
            exit 0
            ;;
        *)
            echo "[sprint1_ab] ERROR: unknown arg: $1" >&2
            exit 2
            ;;
    esac
done

case "$ONLY" in
    ""|"q8_0"|"q4_0") ;;
    *)
        echo "[sprint1_ab] ERROR: --only must be q8_0 or q4_0" >&2
        exit 2
        ;;
esac

TS="$(date -u +%Y%m%dT%H%M%SZ)"
LOG_ROOT="data/baselines"
mkdir -p "$LOG_ROOT"
AB_LOG="$LOG_ROOT/sprint1_ab_${TS}.log"

log() { echo "[sprint1_ab $(date -u +%H:%M:%S)] $*" | tee -a "$AB_LOG"; }

# ─── Pre-flight ─────────────────────────────────────────────────────

log "Sprint 1 KV-quant A/B run starting"
log "  log file: $AB_LOG"
log "  REPO_ROOT: $REPO_ROOT"

if ! command -v docker >/dev/null 2>&1; then
    log "FATAL: docker not in PATH"
    exit 2
fi
if [[ -z "${AMOR_BASELINE_USERNAME:-}" ]]; then
    log "WARN: AMOR_BASELINE_USERNAME unset — Sprint 0 auth may fail"
fi
if [[ -z "${AMOR_BASELINE_PASSWORD:-}" ]]; then
    log "WARN: AMOR_BASELINE_PASSWORD unset — Sprint 0 auth may fail"
fi

# ─── One-pass workflow ──────────────────────────────────────────────

run_variant() {
    local quant="$1"
    log "════════════════════════════════════════════════════════"
    log "VARIANT: $quant — starting pass"
    log "════════════════════════════════════════════════════════"

    # 1. Activate variant
    if ! python3 tools/llamaswap/select_kv_quant.py --quant "$quant" 2>&1 | tee -a "$AB_LOG"; then
        log "FATAL: select_kv_quant.py failed for $quant"
        return 1
    fi

    # 2. Recreate llama-swap with the new config
    log "Recreating amor-llama-swap..."
    docker rm -f amor-llama-swap >/dev/null 2>&1 || true
    if ! docker compose up -d llama-swap 2>&1 | tee -a "$AB_LOG"; then
        log "FATAL: compose up llama-swap failed"
        return 1
    fi

    # Wait up to 120s for /health
    for i in $(seq 1 120); do
        if curl -fsS http://localhost:9100/health >/dev/null 2>&1; then
            log "llama-swap ready after ${i}s"
            break
        fi
        sleep 1
    done
    if ! curl -fsS http://localhost:9100/health >/dev/null 2>&1; then
        log "FATAL: llama-swap /health didn't come up within 120s"
        return 1
    fi

    # 3. Cache-reuse probe
    log "Running cache-reuse probe..."
    if ! python3 tools/llamaswap/probe_cache_reuse.py \
            --base-url http://localhost:9100 \
            --model amor-editor 2>&1 | tee -a "$AB_LOG"; then
        log "FATAL: cache-reuse probe failed for $quant — ABORTING"
        return 1
    fi

    # 4. Sprint 0 v18 run (~6 hours)
    log "Kicking off Sprint 0 baseline (~6h Mistral pass)..."
    AMOR_SPRINT0_JUDGE=mistral \
    AMOR_LLM_BACKEND=llama-swap \
    bash tools/run_sprint0_v18.sh 2>&1 | tee -a "$AB_LOG"
    local rc=${PIPESTATUS[0]}

    # 5. Persist variant-tagged result
    local out="$LOG_ROOT/sprint1_${quant}_results.json"
    if [[ -f data/baselines/sprint0_latest.json ]]; then
        cp data/baselines/sprint0_latest.json "$out"
        log "persisted: $out"
    else
        log "WARN: data/baselines/sprint0_latest.json missing — no scorecard to persist"
        return 1
    fi

    log "VARIANT $quant — pass complete (exit $rc)"
    return "$rc"
}

# ─── Execute ────────────────────────────────────────────────────────

OVERALL_RC=0

if [[ -z "$ONLY" || "$ONLY" == "q8_0" ]]; then
    run_variant "q8_0" || OVERALL_RC=1
fi

if [[ -z "$ONLY" || "$ONLY" == "q4_0" ]]; then
    run_variant "q4_0" || OVERALL_RC=1
fi

# ─── Summary ────────────────────────────────────────────────────────

log "════════════════════════════════════════════════════════"
log "Sprint 1 A/B run complete  (overall rc=$OVERALL_RC)"
log "════════════════════════════════════════════════════════"
log "Scorecards:"
for q in q8_0 q4_0; do
    f="$LOG_ROOT/sprint1_${q}_results.json"
    if [[ -f "$f" ]]; then
        log "  ✓ $f"
    else
        log "  ✗ $f  (not produced)"
    fi
done
log ""
log "Next: write docs/sprint1_decision.md applying the Pareto rule"
log "      from docs/cycle_e_active.md."
exit "$OVERALL_RC"
