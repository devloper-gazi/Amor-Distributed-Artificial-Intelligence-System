#!/usr/bin/env python3
"""
Cycle F Sprint 1 — KV-quant variant selector for llama-swap.

Reads the AMOR_KV_QUANT env var (`q4_0` | `q8_0`, default `q8_0`)
and atomically swaps `compose/llama-swap/config.yaml` to point at
the chosen variant (`config.q4_0.yaml` or `config.q8_0.yaml`).
Designed to be called BEFORE `docker compose up llama-swap`.

On POSIX hosts we create a symlink; on Windows hosts we copy (the
filesystem may not support symlinks, and llama-swap mounts the
file with a bind-mount which prefers a real file anyway).

The previous file is backed up to `config.prev.yaml` so the
operator can rollback with `--rollback`.

Exit codes:
  0  swap succeeded (or already pointing at the requested variant)
  1  unknown quant requested
  2  variant file missing
"""

from __future__ import annotations

import argparse
import os
import shutil
import sys
from pathlib import Path

if sys.platform == "win32":
    for _s in (sys.stdout, sys.stderr):
        _r = getattr(_s, "reconfigure", None)
        if _r is not None:
            try:
                _r(encoding="utf-8", errors="replace")
            except (OSError, ValueError):
                pass

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
COMPOSE_DIR = REPO_ROOT / "compose" / "llama-swap"
ACTIVE = COMPOSE_DIR / "config.yaml"
PREV = COMPOSE_DIR / "config.prev.yaml"
VALID = ("q4_0", "q8_0")


def _variant_path(name: str) -> Path:
    return COMPOSE_DIR / f"config.{name}.yaml"


def _is_pointing_at(active: Path, target: Path) -> bool:
    """True if `active` already mirrors `target` byte-for-byte."""

    try:
        return active.read_bytes() == target.read_bytes()
    except OSError:
        return False


def swap(quant: str) -> int:
    if quant not in VALID:
        print(f"[select_kv_quant] ERROR: unknown quant '{quant}'.  "
              f"Known: {', '.join(VALID)}", file=sys.stderr)
        return 1

    variant = _variant_path(quant)
    if not variant.is_file():
        print(f"[select_kv_quant] ERROR: variant file missing: {variant}",
              file=sys.stderr)
        return 2

    if ACTIVE.is_file() and _is_pointing_at(ACTIVE, variant):
        print(f"[select_kv_quant] active config already = {quant} ✓")
        return 0

    # Back up the current active file (for `--rollback`).
    if ACTIVE.is_file():
        shutil.copy2(ACTIVE, PREV)

    # Atomic-ish copy: write to a sibling temp file then rename.
    tmp = ACTIVE.with_suffix(".yaml.tmp")
    shutil.copy2(variant, tmp)
    os.replace(tmp, ACTIVE)
    print(f"[select_kv_quant] active config -> {quant}  ({variant.name})")
    print(f"[select_kv_quant] previous backed up to {PREV.name}")
    return 0


def rollback() -> int:
    if not PREV.is_file():
        print("[select_kv_quant] no previous config to roll back to.",
              file=sys.stderr)
        return 1
    tmp = ACTIVE.with_suffix(".yaml.tmp")
    shutil.copy2(PREV, tmp)
    os.replace(tmp, ACTIVE)
    print(f"[select_kv_quant] rolled back to {PREV.name}")
    return 0


def status() -> int:
    if not ACTIVE.is_file():
        print("[select_kv_quant] no active config.yaml")
        return 1
    for q in VALID:
        v = _variant_path(q)
        if v.is_file() and _is_pointing_at(ACTIVE, v):
            print(f"[select_kv_quant] active = {q}")
            return 0
    print("[select_kv_quant] active config.yaml does not match a known variant")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Select KV-quant variant for llama-swap."
    )
    parser.add_argument(
        "--quant",
        default=os.environ.get("AMOR_KV_QUANT", "q8_0"),
        choices=VALID,
        help="KV-quant variant to activate (default: env AMOR_KV_QUANT or q8_0).",
    )
    parser.add_argument(
        "--rollback", action="store_true",
        help="Restore config.prev.yaml as the active config.",
    )
    parser.add_argument(
        "--status", action="store_true",
        help="Show which variant the active config matches.",
    )
    args = parser.parse_args()

    if args.status:
        return status()
    if args.rollback:
        return rollback()
    return swap(args.quant)


if __name__ == "__main__":
    raise SystemExit(main())
