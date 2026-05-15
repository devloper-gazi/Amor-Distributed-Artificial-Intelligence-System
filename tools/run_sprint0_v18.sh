#!/usr/bin/env bash
# Cycle E v18 — Sprint 0 baseline runner (overnight-friendly).
#
# Pre-flight: judge GGUF must be downloaded into the
# amor_custom-models-data volume.  Use:
#
#     docker exec amor-app-1 sh -c \
#       'mkdir -p /data/custom_models/judge && cd /data/custom_models/judge && \
#        hf download bartowski/Mistral-Small-24B-Instruct-2501-GGUF \
#          --include "*Q4_K_M*" --local-dir .'
#
# Usage (defaults select Mistral-Small-3-24B Q4_K_M, position-swap on):
#
#     export AMOR_BASELINE_USERNAME=amor-baseline-runner
#     export AMOR_BASELINE_PASSWORD='<vault-secret>'
#     tools/run_sprint0_v18.sh                          # mistral (default)
#     AMOR_SPRINT0_JUDGE=phi4 tools/run_sprint0_v18.sh  # phi-4 fallback
#
# Exit codes:
#   0  — every prompt completed AND every judge call succeeded
#   1  — at least one prompt or judge call failed
#   2  — fatal init (judge container failed to start, AMOR unreachable)
#
# Logs:
#   data/baselines/v18_<timestamp>.log      — combined stdout+stderr
#   data/baselines/sprint0_latest.json      — judged baseline snapshot
#   data/baselines/sprint0_<utc>.jsonl      — per-prompt JSONL trace

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")" && cd .. && pwd)"
cd "$REPO_ROOT"

PROFILE="${AMOR_SPRINT0_JUDGE:-mistral}"
TIMESTAMP="$(date -u +%Y%m%dT%H%M%SZ)"
LOGFILE="data/baselines/v18_${TIMESTAMP}.log"
mkdir -p data/baselines

echo "[run_sprint0_v18] profile=$PROFILE  ts=$TIMESTAMP" | tee -a "$LOGFILE"
echo "[run_sprint0_v18] log=$LOGFILE" | tee -a "$LOGFILE"

# ─── 1. Pre-flight: AMOR healthy? ───────────────────────────────────

if ! curl -fsS http://localhost:8000/health >/dev/null 2>&1; then
    echo "[run_sprint0_v18] FATAL: AMOR unreachable at http://localhost:8000/health" | tee -a "$LOGFILE"
    exit 2
fi
echo "[run_sprint0_v18] AMOR healthy ✓" | tee -a "$LOGFILE"

# ─── 2. Start judge container (idempotent) ──────────────────────────

if ! curl -fsS http://localhost:9101/health >/dev/null 2>&1; then
    echo "[run_sprint0_v18] starting judge container ($PROFILE)..." | tee -a "$LOGFILE"
    if ! AMOR_SPRINT0_JUDGE="$PROFILE" tools/judge/select_and_start.sh 2>&1 | tee -a "$LOGFILE"; then
        echo "[run_sprint0_v18] FATAL: judge container failed to start" | tee -a "$LOGFILE"
        exit 2
    fi
    # Wait up to 90s for the model to load + serve /health
    for i in $(seq 1 90); do
        if curl -fsS http://localhost:9101/health >/dev/null 2>&1; then
            echo "[run_sprint0_v18] judge ready after ${i}s" | tee -a "$LOGFILE"
            break
        fi
        sleep 1
    done
    if ! curl -fsS http://localhost:9101/health >/dev/null 2>&1; then
        echo "[run_sprint0_v18] FATAL: judge /health didn't come up within 90s" | tee -a "$LOGFILE"
        exit 2
    fi
else
    echo "[run_sprint0_v18] judge already running on :9101 ✓" | tee -a "$LOGFILE"
fi

# ─── 3. Run baseline + judge ────────────────────────────────────────

# Resolve auth from env-var or .env file.  AMOR baseline uses
# username/password (token rotates) — env vars take precedence.
USERNAME="${AMOR_BASELINE_USERNAME:-amor-baseline-runner}"
PASSWORD="${AMOR_BASELINE_PASSWORD:-}"
if [[ -z "$PASSWORD" ]]; then
    echo "[run_sprint0_v18] WARN: AMOR_BASELINE_PASSWORD unset; trying token-only auth" | tee -a "$LOGFILE"
fi

# `script -e -c '...' /dev/null` on Linux preserves color + exit code;
# Windows Git Bash uses tee.  Pick the simpler path: tee.
START_TS="$(date -u +%s)"
set +e
python3 tools/run_sprint0_baseline.py \
    --base-url http://localhost:8000 \
    --auth-username "$USERNAME" \
    ${PASSWORD:+--auth-password "$PASSWORD"} \
    --judge-profile "$PROFILE" \
    --judge-url http://localhost:9101 \
    --judge-timeout-s 600 \
    --backend llama-cpp \
    2>&1 | tee -a "$LOGFILE"
RC=${PIPESTATUS[0]}
set -e
END_TS="$(date -u +%s)"
DUR=$((END_TS - START_TS))

# ─── 4. Tear down judge (always) ────────────────────────────────────

echo "[run_sprint0_v18] stopping judge container..." | tee -a "$LOGFILE"
docker rm -f amor-judge >/dev/null 2>&1 || true

# ─── 5. Summary ─────────────────────────────────────────────────────

echo "" | tee -a "$LOGFILE"
echo "═══════════════════════════════════════════════════════════════" | tee -a "$LOGFILE"
echo "Sprint 0 v18 baseline run complete" | tee -a "$LOGFILE"
echo "  profile : $PROFILE" | tee -a "$LOGFILE"
echo "  exit    : $RC" | tee -a "$LOGFILE"
echo "  wall    : ${DUR}s" | tee -a "$LOGFILE"
echo "  log     : $LOGFILE" | tee -a "$LOGFILE"
echo "  result  : data/baselines/sprint0_latest.json" | tee -a "$LOGFILE"
echo "═══════════════════════════════════════════════════════════════" | tee -a "$LOGFILE"

# Quick sanity: summarize judge means from latest.json
if [[ -f "data/baselines/sprint0_latest.json" ]]; then
    python3 - "$LOGFILE" <<'PY' || true
import json, sys, statistics
log = sys.argv[1]
with open("data/baselines/sprint0_latest.json", encoding="utf-8") as f:
    data = json.load(f)
rows = data.get("rows", [])
correct = [r["judge_score"]["correctness"] for r in rows
           if isinstance(r.get("judge_score"), dict) and "correctness" in r["judge_score"]]
complet = [r["judge_score"]["completeness"] for r in rows
           if isinstance(r.get("judge_score"), dict) and "completeness" in r["judge_score"]]
uncertain = [r for r in rows if isinstance(r.get("judge_score"), dict) and r["judge_score"].get("uncertain")]
errored = [r for r in rows if isinstance(r.get("judge_score"), dict) and "error" in r["judge_score"]]

def writeln(line):
    print(line)
    with open(log, "a", encoding="utf-8") as f:
        f.write(line + "\n")

writeln("")
writeln("Judge summary:")
writeln(f"  rows       : {len(rows)}")
writeln(f"  judged     : {len(correct)}")
writeln(f"  uncertain  : {len(uncertain)}")
writeln(f"  errored    : {len(errored)}")
if correct:
    writeln(f"  correctness: mean={statistics.mean(correct):.2f} stdev={statistics.stdev(correct) if len(correct)>1 else 0:.2f} range=[{min(correct)}..{max(correct)}]")
if complet:
    writeln(f"  completeness: mean={statistics.mean(complet):.2f} stdev={statistics.stdev(complet) if len(complet)>1 else 0:.2f} range=[{min(complet)}..{max(complet)}]")
PY
fi

exit $RC
