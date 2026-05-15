"""Cycle E v18 — cross-platform setup orchestrator for AMOR.

Modules:
  constants  — single source of truth for services / ports / profiles
  util       — OS detection, subprocess wrapper, color, spinner, probes
  preflight  — pre-install checks (Docker / disk / RAM / GPU / ports)
  envfile    — idempotent .env management
  compose    — docker compose v1/v2 + overlay file detection
  health     — health-check polling with retry + backoff
  models     — judge GGUF + ollama / llama-swap model bootstrap
  doctor     — diagnostics report + remediation hints
  services   — start / stop / restart / status / logs
  verify     — live smoke against the running stack
  cli        — argparse subcommand dispatch (entry point)

Usage:
  python -m tools.setup install            # full bootstrap
  python -m tools.setup doctor             # diagnose
  python -m tools.setup start              # start services
  python -m tools.setup verify             # smoke the running stack

Conventions:
  * Stdlib-only at import time so the module loads before pip install.
  * Idempotent: every command is safe to re-run.
  * Exit codes:
       0  success
       1  recoverable failure (with remediation hint)
       2  fatal preflight failure (Docker missing, disk full, etc.)
"""

__version__ = "18.0.0"
