#!/usr/bin/env bash
# AMOR — legacy `start.sh` entry point.
#
# Cycle E v18 replaced this script with `tools/setup/` (a Python
# orchestrator with preflight + idempotent install + verify).  This
# stub simply forwards to `setup.sh start` so existing muscle memory
# (./start.sh) still works.

set -euo pipefail
REPO_ROOT="$(cd "$(dirname "$0")" && pwd)"
exec "$REPO_ROOT/setup.sh" start "$@"
