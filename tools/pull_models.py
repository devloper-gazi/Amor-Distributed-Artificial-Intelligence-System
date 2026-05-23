#!/usr/bin/env python3
"""
Cycle C Sprint 1 Day 1 — pull AMOR's llama-swap GGUFs from Hugging Face.

Three models, all Q4_K_M:
  unsloth/DeepSeek-R1-0528-Qwen3-8B-GGUF      — ~5 GB  (architect)
  unsloth/Qwen2.5-Coder-7B-Instruct-GGUF      — ~4.7 GB (editor)
  unsloth/Qwen3-8B-GGUF                       — ~5 GB  (fast / non-thinking)

Idempotent: if the target file already exists at the expected size, skip
the download.  Resume-aware: ``huggingface_hub.snapshot_download``
handles incomplete chunks via .cache.

Target dir: ``data/custom_models/llamaswap/`` on the named volume
``amor_custom-models-data`` — the same volume the judge GGUF lives in,
under a separate subdir so they don't collide.

Usage::

    # Locally on the host (writes to volume via a busybox helper):
    python tools/pull_models.py

    # Inside the app container (volume already mounted at /data/...):
    docker exec amor-app-1 python /app/tools/pull_models.py

Override which models with ``--only architect`` / ``--only editor`` etc.
Override target dir with ``--out /custom/path``.

Environment overrides:
    AMOR_LLAMASWAP_DIR       default /data/custom_models/llamaswap
    HF_HUB_DISABLE_PROGRESS  set to non-empty for quiet output (CI)

Exit codes:
    0 — every requested model present at expected size
    1 — at least one model failed to materialise
    2 — fatal init error (huggingface_hub missing, dir creation failure)
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("pull_models")


@dataclass(frozen=True)
class ModelSpec:
    """One GGUF target — repo + filename + role tag."""

    role: str             # architect | editor | fast
    repo_id: str          # HF repo id
    filename: str         # exact GGUF filename inside the repo
    expected_min_bytes: int  # sanity floor — partial files trip this
    aliases: tuple[str, ...] = ()


# Sprint 1 manifest — Q4_K_M everywhere; sizes verified May 2026.
MODELS: dict[str, ModelSpec] = {
    "architect": ModelSpec(
        role="architect",
        repo_id="unsloth/DeepSeek-R1-0528-Qwen3-8B-GGUF",
        filename="DeepSeek-R1-0528-Qwen3-8B-Q4_K_M.gguf",
        expected_min_bytes=4_500_000_000,   # ~5 GB
        aliases=("deepseek-r1-qwen3", "amor-architect"),
    ),
    "editor": ModelSpec(
        role="editor",
        repo_id="unsloth/Qwen2.5-Coder-7B-Instruct-GGUF",
        filename="Qwen2.5-Coder-7B-Instruct-Q4_K_M.gguf",
        expected_min_bytes=4_300_000_000,   # ~4.7 GB
        aliases=("qwen-coder", "amor-editor"),
    ),
    "fast": ModelSpec(
        role="fast",
        repo_id="unsloth/Qwen3-8B-GGUF",
        filename="Qwen3-8B-Q4_K_M.gguf",
        expected_min_bytes=4_500_000_000,   # ~5 GB
        aliases=("qwen3", "amor-fast"),
    ),
}


def _default_out_dir() -> Path:
    env = os.environ.get("AMOR_LLAMASWAP_DIR")
    if env:
        return Path(env)
    return Path("/data/custom_models/llamaswap")


def _spec_present(out_dir: Path, spec: ModelSpec) -> bool:
    """Return True iff the GGUF is already on disk at >= expected size."""
    target = out_dir / spec.filename
    if not target.is_file():
        return False
    try:
        size = target.stat().st_size
    except OSError:
        return False
    return size >= spec.expected_min_bytes


def _download_one(out_dir: Path, spec: ModelSpec, *, dry_run: bool) -> bool:
    """Download ``spec`` into ``out_dir``.  Returns True on success.

    Uses ``huggingface_hub.snapshot_download`` with an
    ``allow_patterns`` pin so we don't slurp the whole repo (some
    of these have 8+ quant variants × 5+ GB each).
    """
    if _spec_present(out_dir, spec):
        logger.info("%-10s already present: %s", spec.role, spec.filename)
        return True
    if dry_run:
        logger.info("%-10s would download: %s/%s",
                    spec.role, spec.repo_id, spec.filename)
        return True
    try:
        from huggingface_hub import snapshot_download  # noqa: PLC0415
    except ImportError:
        logger.error(
            "huggingface_hub missing — install via "
            "`pip install huggingface_hub>=0.26`",
        )
        return False

    logger.info("%-10s downloading %s …", spec.role, spec.filename)
    try:
        snapshot_download(
            repo_id=spec.repo_id,
            allow_patterns=[spec.filename],
            local_dir=str(out_dir),
            local_dir_use_symlinks=False,   # tighter on Windows hosts
        )
    except Exception as exc:
        logger.error("%-10s download failed: %s", spec.role, exc)
        return False

    if not _spec_present(out_dir, spec):
        logger.error(
            "%-10s post-download size check failed (target: %s)",
            spec.role, out_dir / spec.filename,
        )
        return False

    size_gb = (out_dir / spec.filename).stat().st_size / 1e9
    logger.info("%-10s settled at %.2f GB", spec.role, size_gb)
    return True


def main(argv: Optional[list[str]] = None) -> int:
    p = argparse.ArgumentParser(prog="pull_models")
    p.add_argument(
        "--out",
        default=str(_default_out_dir()),
        help=(
            "Target directory for GGUF files "
            "(default: /data/custom_models/llamaswap)"
        ),
    )
    p.add_argument(
        "--only",
        action="append",
        default=None,
        choices=sorted(MODELS.keys()) + ["all"],
        help=(
            "Restrict download to one or more roles (architect/editor/fast). "
            "Default: all three."
        ),
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        default=False,
        help="Print what would be downloaded; touch nothing.",
    )
    args = p.parse_args(argv)

    out_dir = Path(args.out).resolve()
    try:
        out_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        logger.error("cannot create %s: %s", out_dir, exc)
        return 2

    if args.only and "all" not in args.only:
        targets = [MODELS[r] for r in args.only]
    else:
        targets = list(MODELS.values())

    logger.info(
        "out=%s  models=%s  dry_run=%s",
        out_dir,
        ",".join(s.role for s in targets),
        args.dry_run,
    )

    failed = 0
    for spec in targets:
        if not _download_one(out_dir, spec, dry_run=args.dry_run):
            failed += 1

    if failed:
        logger.error("%d/%d model(s) failed to materialise", failed, len(targets))
        return 1
    logger.info("all %d model(s) ready under %s", len(targets), out_dir)
    return 0


if __name__ == "__main__":
    sys.exit(main())
