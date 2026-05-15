#!/usr/bin/env bash
# Cycle C Sprint 5 Day 5 — docker-bench-security wrapper.
#
# Runs Aqua Security's docker-bench-security container against the
# host's Docker daemon, scrapes the WARN/PASS/INFO counts, and writes
# a summary JSON to ``data/security/docker_bench_<utc-iso>.json``.
#
# The full text report lands alongside the JSON for forensic review.
# A one-line summary is echoed to stdout so CI / runbooks can parse it.
#
# Usage::
#
#     ./tools/run_docker_bench.sh
#     ./tools/run_docker_bench.sh --update-baseline
#
# The ``--update-baseline`` flag rewrites
# ``data/security/docker_bench_baseline.json`` with the current run.
# Sprint 5 acceptance: bench score ≥ Sprint-4 baseline + 5 PASS items.
#
# Requires the host docker daemon to be reachable.  When run inside a
# container with ``/var/run/docker.sock`` bind-mounted, results
# reflect the HOST daemon (which is what we want — bench grades the
# daemon, not nested namespaces).

set -euo pipefail

OUT_DIR="${AMOR_DOCKER_BENCH_OUT:-data/security}"
mkdir -p "$OUT_DIR"
TS="$(date -u +%Y%m%dT%H%M%SZ)"
TXT="$OUT_DIR/docker_bench_${TS}.txt"
JSON="$OUT_DIR/docker_bench_${TS}.json"
BASELINE="$OUT_DIR/docker_bench_baseline.json"
UPDATE_BASELINE=0

if [[ "${1:-}" == "--update-baseline" ]]; then
    UPDATE_BASELINE=1
fi

echo "[docker-bench] running against host daemon — output -> $TXT"

# The aquasec/docker-bench-security image needs a few host volumes to
# inspect the daemon properly.  The flags below come from the
# project's official run.sh.
docker run --rm --net host --pid host --userns host --cap-add audit_control \
    -e DOCKER_CONTENT_TRUST="${DOCKER_CONTENT_TRUST:-}" \
    -v /etc:/etc:ro \
    -v /usr/bin/containerd:/usr/bin/containerd:ro \
    -v /usr/bin/runc:/usr/bin/runc:ro \
    -v /usr/lib/systemd:/usr/lib/systemd:ro \
    -v /var/lib:/var/lib:ro \
    -v /var/run/docker.sock:/var/run/docker.sock:ro \
    --label docker_bench_security \
    aquasec/docker-bench-security 2>&1 | tee "$TXT" >/dev/null

# Scrape the colorised summary block for PASS/WARN/INFO counts.
PASS=$(grep -c -E '^\[PASS\]' "$TXT" || true)
WARN=$(grep -c -E '^\[WARN\]' "$TXT" || true)
INFO=$(grep -c -E '^\[INFO\]' "$TXT" || true)
NOTE=$(grep -c -E '^\[NOTE\]' "$TXT" || true)

cat > "$JSON" <<EOF
{
  "schema_version": 1,
  "captured_at_utc": "$(date -u +%Y-%m-%dT%H:%M:%SZ)",
  "txt_report": "$(basename "$TXT")",
  "counts": {
    "pass": ${PASS:-0},
    "warn": ${WARN:-0},
    "info": ${INFO:-0},
    "note": ${NOTE:-0}
  },
  "score": $((${PASS:-0} - ${WARN:-0}))
}
EOF

echo "[docker-bench] summary: PASS=${PASS:-0} WARN=${WARN:-0} INFO=${INFO:-0} NOTE=${NOTE:-0}"
echo "[docker-bench] JSON written to $JSON"

if [[ "$UPDATE_BASELINE" == "1" ]]; then
    cp "$JSON" "$BASELINE"
    echo "[docker-bench] baseline updated -> $BASELINE"
fi

if [[ -f "$BASELINE" ]]; then
    BASE_PASS=$(python3 -c "import json,sys;print(json.load(open('$BASELINE'))['counts']['pass'])")
    DELTA=$((${PASS:-0} - BASE_PASS))
    echo "[docker-bench] PASS delta vs baseline: ${DELTA}"
    if (( DELTA < 5 )); then
        echo "[docker-bench] WARN: Sprint 5 acceptance is +5 PASS items vs Sprint-4 baseline."
    fi
fi
