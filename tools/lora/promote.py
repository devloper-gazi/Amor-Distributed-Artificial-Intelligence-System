#!/usr/bin/env python3
"""
Cycle F Sprint 6 piece 3 — LoRA adapter promote/rollback helper.

Promotes a candidate adapter from
`models/lora/candidate/<role>-r16-<utc>.gguf` to the in-production
slot at `models/lora/<role>-r16.gguf` via atomic symlink (POSIX) /
copy (Windows) swap.  Records the previous adapter at
`models/lora/<role>-r16.prev.gguf` so a single `--rollback` call
reverses the swap.

Workflow:

  1. operator inspects `data/training/diff_<utc>.md`
  2. for each PROMOTE-worthy role, runs:
       python tools/lora/promote.py --role coder \
           --candidate models/lora/candidate/coder-r16-<utc>.gguf \
           --promote
  3. restarts llama-swap so the new adapter is reloaded
  4. verifies via /api/code/diagnostics + a Build session

Does NOT auto-restart llama-swap — that's the operator's call so a
shipping LoRA isn't disturbed mid-stream.
"""

from __future__ import annotations

import argparse
import logging
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

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
logger = logging.getLogger(__name__)


REPO_ROOT = Path(__file__).resolve().parent.parent.parent
LORA_ROOT = REPO_ROOT / "models" / "lora"

VALID_ROLES = ("coder", "tester", "debugger")


# ─── Atomic swap ────────────────────────────────────────────────────


def _active_path(role: str) -> Path:
    return LORA_ROOT / f"{role}-r16.gguf"


def _prev_path(role: str) -> Path:
    return LORA_ROOT / f"{role}-r16.prev.gguf"


def _atomic_replace(src: Path, dst: Path) -> None:
    """Best-effort atomic move.  Windows + POSIX both supported via
    `os.replace` (atomic on same filesystem)."""

    LORA_ROOT.mkdir(parents=True, exist_ok=True)
    # Copy to a sibling temp first so the source isn't lost on a
    # mid-swap crash.
    tmp = dst.with_suffix(".gguf.tmp")
    shutil.copy2(src, tmp)
    os.replace(tmp, dst)


def promote(role: str, candidate: Path) -> int:
    """Swap `candidate` into the active slot.  Saves the previous
    active as `<role>-r16.prev.gguf` for `--rollback`."""

    if role not in VALID_ROLES:
        logger.error("unknown role %r (known: %s)", role, VALID_ROLES)
        return 1
    if not candidate.is_file():
        logger.error("candidate adapter missing: %s", candidate)
        return 1

    active = _active_path(role)
    prev = _prev_path(role)

    if active.is_file():
        logger.info("backing up current active -> %s", prev.name)
        _atomic_replace(active, prev)

    logger.info("promoting %s -> %s", candidate, active)
    _atomic_replace(candidate, active)
    logger.info("promote complete.  Restart amor-llama-swap to load.")
    return 0


def rollback(role: str) -> int:
    """Restore `<role>-r16.prev.gguf` as the active adapter."""

    if role not in VALID_ROLES:
        logger.error("unknown role %r (known: %s)", role, VALID_ROLES)
        return 1
    prev = _prev_path(role)
    if not prev.is_file():
        logger.error("no prev adapter to roll back to at %s", prev)
        return 1
    active = _active_path(role)
    logger.info("rolling back %s -> %s", prev, active)
    _atomic_replace(prev, active)
    try:
        prev.unlink()
    except OSError:
        pass
    logger.info("rollback complete.  Restart amor-llama-swap to load.")
    return 0


def status() -> int:
    """Print which adapters are currently in-production + which have
    a roll-backable predecessor."""

    LORA_ROOT.mkdir(parents=True, exist_ok=True)
    print(f"LoRA root: {LORA_ROOT}")
    print()
    print(f"{'role':<10s} {'active':<25s} {'has_prev':<10s}")
    print("-" * 50)
    for role in VALID_ROLES:
        active = _active_path(role)
        prev = _prev_path(role)
        a_str = active.name if active.is_file() else "—"
        p_str = "yes" if prev.is_file() else "—"
        print(f"{role:<10s} {a_str:<25s} {p_str:<10s}")
    return 0


# ─── CLI ────────────────────────────────────────────────────────────


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = p.add_subparsers(dest="cmd", required=False)

    p.add_argument(
        "--status", action="store_true",
        help="Show the current adapter inventory (default action).",
    )

    promote_p = sub.add_parser("promote", help="Promote a candidate adapter.")
    promote_p.add_argument("--role", required=True, choices=VALID_ROLES)
    promote_p.add_argument(
        "--candidate", required=True,
        help="Path to the candidate adapter GGUF.",
    )

    rollback_p = sub.add_parser("rollback", help="Restore the previous adapter.")
    rollback_p.add_argument("--role", required=True, choices=VALID_ROLES)

    return p


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    if args.cmd == "promote":
        return promote(args.role, Path(args.candidate))
    if args.cmd == "rollback":
        return rollback(args.role)
    # Default: status
    return status()


if __name__ == "__main__":
    raise SystemExit(main())
