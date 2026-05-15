#!/usr/bin/env python3
"""
Cycle C Sprint 6 Day 2 — PEFT → GGUF LoRA converter.

Wraps llama.cpp's ``convert-lora-to-gguf.py`` so the trained adapter
from ``orpo_qwen_coder.py`` can be hot-swapped into llama-server.

Usage
-----

    python tools/training/convert_lora_gguf.py \\
        --peft  /app/models/lora/amor-orpo-2026-05-04 \\
        --base  /app/models/Qwen2.5-Coder-7B-Instruct-Q4_K_M.gguf \\
        --out   /app/models/lora/amor-orpo-2026-05-04.gguf \\
        --llama-cpp /opt/llama.cpp

Resolution
----------
``--llama-cpp`` defaults to ``$LLAMA_CPP_DIR``, ``/opt/llama.cpp``,
or the directory the converter ships with the llama-swap container.
We don't vendor llama.cpp; the operator provides the path.

Hot-load
--------
After this writes the GGUF, llama-server picks it up via::

    POST /v1/lora-adapters
    [{"id": 0, "scale": 1.0}]

with ``--lora /app/models/lora/amor-orpo-2026-05-04.gguf
--lora-init-without-apply`` already present in the llama-server cmd.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import shutil
import subprocess
import sys
from pathlib import Path

logger = logging.getLogger(__name__)


CANDIDATE_LLAMA_CPP_DIRS: list[str] = [
    os.environ.get("LLAMA_CPP_DIR", ""),
    "/opt/llama.cpp",
    "/usr/local/llama.cpp",
    "/llama.cpp",
]


def find_converter(explicit: str | None) -> Path | None:
    """Locate ``convert-lora-to-gguf.py`` under the requested
    llama.cpp tree.  Returns None when nothing is found."""
    candidates: list[str] = []
    if explicit:
        candidates.append(explicit)
    candidates.extend([d for d in CANDIDATE_LLAMA_CPP_DIRS if d])

    for base in candidates:
        p = Path(base) / "convert-lora-to-gguf.py"
        if p.is_file():
            return p
        # llama.cpp moved the script under ``convert/`` in some forks.
        p2 = Path(base) / "convert" / "convert-lora-to-gguf.py"
        if p2.is_file():
            return p2
    return None


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Convert a PEFT LoRA adapter to llama.cpp GGUF.",
    )
    p.add_argument("--peft", required=True, help="PEFT adapter directory")
    p.add_argument("--base", required=True, help="base GGUF (Qwen2.5-Coder-7B …)")
    p.add_argument("--out", required=True, help="destination GGUF path")
    p.add_argument(
        "--llama-cpp",
        default=None,
        help="llama.cpp source directory (looks up convert-lora-to-gguf.py)",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="print the resolved command + check inputs without invoking it",
    )
    return p


def main() -> int:
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"))
    args = build_parser().parse_args()

    peft_dir = Path(args.peft)
    base_path = Path(args.base)
    out_path = Path(args.out)
    if not peft_dir.is_dir():
        logger.error("PEFT directory missing: %s", peft_dir)
        return 2
    if not base_path.is_file():
        logger.error("base GGUF missing: %s", base_path)
        return 2

    converter = find_converter(args.llama_cpp)
    if converter is None:
        logger.error(
            "convert-lora-to-gguf.py not found under any of: %s",
            ", ".join(filter(None, [args.llama_cpp, *CANDIDATE_LLAMA_CPP_DIRS])),
        )
        return 2

    out_path.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        sys.executable,
        str(converter),
        str(peft_dir),
        "--outfile",
        str(out_path),
        "--base",
        str(base_path),
    ]
    summary = {
        "converter": str(converter),
        "command": cmd,
        "peft": str(peft_dir),
        "base": str(base_path),
        "out": str(out_path),
    }
    if args.dry_run:
        print(json.dumps({"dry_run": True, **summary}, indent=2))
        return 0

    logger.info("running: %s", " ".join(cmd))
    proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if proc.returncode != 0:
        logger.error("converter failed (rc=%s):\n%s", proc.returncode, proc.stderr)
        return proc.returncode
    print(json.dumps({"ok": True, **summary}, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
