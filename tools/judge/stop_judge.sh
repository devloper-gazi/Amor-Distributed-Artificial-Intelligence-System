#!/usr/bin/env bash
# Tear down the amor-judge llama-server container started by
# tools/judge/start_judge.sh.  Safe to run when the container isn't
# present (returns 0).
set -euo pipefail
docker rm -f amor-judge >/dev/null 2>&1 || true
echo "amor-judge stopped"
