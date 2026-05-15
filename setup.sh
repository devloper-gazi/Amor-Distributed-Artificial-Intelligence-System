#!/usr/bin/env bash
# AMOR — POSIX bootstrap shim.  Linux + macOS.
#
# Locates python3 (or python ≥3.9), then hands off to the
# `tools.setup` package which does the real work.  Pass any args
# straight through:
#
#     ./setup.sh                        # default: install (full profile)
#     ./setup.sh install --profile dev
#     ./setup.sh doctor
#     ./setup.sh verify
#     ./setup.sh start
#     ./setup.sh stop
#     ./setup.sh status
#     ./setup.sh logs app -f

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")" && pwd)"
cd "$REPO_ROOT"

# ─── Pick a Python ≥ 3.9 ───────────────────────────────────────────

choose_python() {
    for candidate in python3 python; do
        if command -v "$candidate" >/dev/null 2>&1; then
            ver=$("$candidate" -c \
                'import sys; print(".".join(map(str, sys.version_info[:2])))' \
                2>/dev/null || echo "0.0")
            major=${ver%%.*}
            minor=${ver##*.}
            if [[ "$major" -ge 3 && "$minor" -ge 9 ]]; then
                echo "$candidate"
                return 0
            fi
        fi
    done
    return 1
}

PY="$(choose_python || true)"
if [[ -z "${PY:-}" ]]; then
    echo "[setup.sh] ERROR: Python 3.9+ not found on PATH." >&2
    echo "[setup.sh]   macOS:   brew install python@3.12" >&2
    echo "[setup.sh]   Debian:  sudo apt-get install python3" >&2
    echo "[setup.sh]   Fedora:  sudo dnf install python3" >&2
    exit 127
fi

# ─── Default command: install ──────────────────────────────────────

if [[ "$#" -eq 0 ]]; then
    set -- install
fi

# ─── Run ───────────────────────────────────────────────────────────

exec "$PY" -m tools.setup "$@"
