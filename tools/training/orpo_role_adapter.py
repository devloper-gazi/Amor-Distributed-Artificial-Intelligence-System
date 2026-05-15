#!/usr/bin/env python3
"""
Cycle F Sprint 3 — role-specific ORPO LoRA training driver.

A thin wrapper around `orpo_qwen_coder.py` that pins the Cycle F
recipe (r=16, alpha=32) and routes the corpus + output paths by
role.  One run produces ONE adapter for ONE role.

The base model stays Qwen2.5-Coder-7B-Instruct — all three roles
(coder / tester / debugger) share the same base because the engine
selects the adapter via per-request `"lora": [...]` rather than
swapping a separate model.

Usage::

    # Default: corpus at data/preference_pairs/coder.jsonl, out at
    # models/lora/coder-r16/, then convert to GGUF in-place.
    python tools/training/orpo_role_adapter.py --role coder

    # Custom corpus + output:
    python tools/training/orpo_role_adapter.py \\
        --role tester \\
        --jsonl data/preference_pairs/tester_2026-05-12.jsonl \\
        --out   models/lora/tester-r16

    # Dry run (validates env, no GPU touch):
    python tools/training/orpo_role_adapter.py --role coder --dry-run

After training, convert PEFT → GGUF via the existing helper:

    python tools/training/convert_lora_gguf.py \\
        --peft models/lora/coder-r16 \\
        --out  models/lora/coder-r16.gguf

Then uncomment the matching `--lora-init-without-apply` line in
`compose/llama-swap/config.yaml` and restart llama-swap.

Recipe rationale (locked across roles):

  * `r=16, alpha=32` — rsLoRA arXiv 2312.03732 sweet spot.  Captures
    most of the specialization benefit at fraction of the training
    cost vs higher ranks.
  * `dropout=0.05`, 7 target modules (q/k/v/o + gate/up/down proj)
  * `max_seq_length=2048` — VRAM is linear in seq_len; 2048 fits
    comfortably on 8 GiB with bs=1, grad_accum=4.
  * `lr=8e-6` cosine, warmup_ratio=0.1, beta=0.1, 1 epoch.
  * Effective batch = 4 (bs=1 x grad_accum=4); paged-adamw-8bit
    optimiser to avoid VRAM spikes.

Wall-clock budget on RTX 4060 8 GiB: ~20-35 min per 200 pairs
(extrapolated from Cycle C measurements; flag as [ESTIMATED] until
the first real run logs land).
"""

from __future__ import annotations

import argparse
import logging
import os
import subprocess
import sys
from pathlib import Path

logger = logging.getLogger(__name__)


REPO_ROOT = Path(__file__).resolve().parent.parent.parent

# Canonical adapter ID assignments (must match the mount order in
# compose/llama-swap/config.yaml --lora-init-without-apply lines).
ROLE_ADAPTER_IDS: dict[str, int] = {
    "coder": 0,
    "tester": 1,
    "debugger": 2,
}

# Cycle F locked recipe.
DEFAULT_LORA_R = 16
DEFAULT_LORA_ALPHA = 32  # rsLoRA: alpha = 2 * r
DEFAULT_LORA_DROPOUT = 0.05
DEFAULT_LR = 8e-6
DEFAULT_BETA = 0.1
DEFAULT_EPOCHS = 1.0
DEFAULT_MAX_SEQ_LENGTH = 2048
DEFAULT_BATCH_SIZE = 1
DEFAULT_GRAD_ACCUM = 4
DEFAULT_MAX_PROMPT_LENGTH = 512
DEFAULT_MIN_PAIRS = 50  # below this, training is noise


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument(
        "--role",
        required=True,
        choices=sorted(ROLE_ADAPTER_IDS.keys()),
        help="Which role to train an adapter for.",
    )
    p.add_argument(
        "--jsonl",
        default=None,
        help=("Preference-pair JSONL.  Default: "
              "data/preference_pairs/{role}.jsonl"),
    )
    p.add_argument(
        "--out",
        default=None,
        help=("PEFT output directory.  Default: "
              "models/lora/{role}-r{R}"),
    )
    p.add_argument(
        "--model-name",
        default="unsloth/Qwen2.5-Coder-7B-Instruct-bnb-4bit",
        help="HuggingFace model name or local path for the base.",
    )
    p.add_argument("--r", "--lora-r", dest="lora_r",
                   type=int, default=DEFAULT_LORA_R)
    p.add_argument("--alpha", "--lora-alpha", dest="lora_alpha",
                   type=int, default=DEFAULT_LORA_ALPHA)
    p.add_argument("--dropout", "--lora-dropout", dest="lora_dropout",
                   type=float, default=DEFAULT_LORA_DROPOUT)
    p.add_argument("--lr", type=float, default=DEFAULT_LR)
    p.add_argument("--beta", type=float, default=DEFAULT_BETA)
    p.add_argument("--epochs", type=float, default=DEFAULT_EPOCHS)
    p.add_argument("--max-seq-length", type=int, default=DEFAULT_MAX_SEQ_LENGTH)
    p.add_argument("--batch-size", type=int, default=DEFAULT_BATCH_SIZE)
    p.add_argument("--grad-accum", type=int, default=DEFAULT_GRAD_ACCUM)
    p.add_argument("--max-prompt-length", type=int, default=DEFAULT_MAX_PROMPT_LENGTH)
    p.add_argument(
        "--min-pairs", type=int, default=DEFAULT_MIN_PAIRS,
        help=(f"Refuse to train below this many pairs (default "
              f"{DEFAULT_MIN_PAIRS}).  Pass --allow-tiny to bypass."),
    )
    p.add_argument(
        "--allow-tiny", action="store_true",
        help="Bypass --min-pairs (smoke tests).",
    )
    p.add_argument(
        "--convert-gguf", action="store_true",
        help=("After training, run convert_lora_gguf.py to emit "
              "models/lora/{role}-r{R}.gguf alongside the PEFT dir."),
    )
    p.add_argument(
        "--dry-run", action="store_true",
        help="Resolve paths + validate env; do not invoke the trainer.",
    )
    return p


def resolve_paths(args: argparse.Namespace) -> tuple[Path, Path]:
    """Compute (jsonl, out) from --role + defaults."""

    role = args.role
    jsonl = (
        Path(args.jsonl)
        if args.jsonl
        else REPO_ROOT / "data" / "preference_pairs" / f"{role}.jsonl"
    )
    out = (
        Path(args.out)
        if args.out
        else REPO_ROOT / "models" / "lora" / f"{role}-r{args.lora_r}"
    )
    return jsonl, out


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    parser = build_parser()
    args = parser.parse_args()

    role = args.role
    jsonl, out_dir = resolve_paths(args)
    adapter_id = ROLE_ADAPTER_IDS[role]

    logger.info("orpo_role_adapter: role=%s adapter_id=%d", role, adapter_id)
    logger.info("orpo_role_adapter:   jsonl  = %s", jsonl)
    logger.info("orpo_role_adapter:   out    = %s", out_dir)
    logger.info("orpo_role_adapter:   r=%d alpha=%d dropout=%s",
                args.lora_r, args.lora_alpha, args.lora_dropout)
    logger.info("orpo_role_adapter:   lr=%s beta=%s epochs=%s",
                args.lr, args.beta, args.epochs)

    # Pre-flight: corpus exists + min-pairs gate.
    if not jsonl.is_file():
        logger.error("orpo_role_adapter: corpus not found: %s", jsonl)
        return 2
    try:
        n_pairs = sum(1 for line in jsonl.open(encoding="utf-8") if line.strip())
    except OSError as exc:
        logger.error("orpo_role_adapter: cannot read corpus: %s", exc)
        return 2
    logger.info("orpo_role_adapter: pair count = %d", n_pairs)
    if n_pairs < args.min_pairs and not args.allow_tiny:
        logger.error(
            "orpo_role_adapter: refusing to train on %d pairs "
            "(min %d).  Pass --allow-tiny to bypass.",
            n_pairs, args.min_pairs,
        )
        return 2

    if args.dry_run:
        logger.info("orpo_role_adapter: --dry-run, exiting before trainer.")
        return 0

    # Hand off to the existing Cycle C trainer with role-specific overrides.
    trainer = REPO_ROOT / "tools" / "training" / "orpo_qwen_coder.py"
    if not trainer.is_file():
        logger.error("orpo_role_adapter: trainer missing: %s", trainer)
        return 2

    cmd = [
        sys.executable, str(trainer),
        "--jsonl", str(jsonl),
        "--out", str(out_dir),
        "--model-name", args.model_name,
        "--max-seq-length", str(args.max_seq_length),
        "--epochs", str(args.epochs),
        "--batch-size", str(args.batch_size),
        "--grad-accum", str(args.grad_accum),
        "--lr", str(args.lr),
        "--lora-r", str(args.lora_r),
        "--lora-alpha", str(args.lora_alpha),
        "--lora-dropout", str(args.lora_dropout),
        "--beta", str(args.beta),
        "--max-prompt-length", str(args.max_prompt_length),
        "--min-pairs", str(args.min_pairs),
    ]
    if args.allow_tiny:
        cmd.append("--allow-tiny")

    logger.info("orpo_role_adapter: launching trainer subprocess...")
    rc = subprocess.call(cmd, cwd=str(REPO_ROOT))
    if rc != 0:
        logger.error("orpo_role_adapter: trainer exited with code %d", rc)
        return rc

    if args.convert_gguf:
        converter = REPO_ROOT / "tools" / "training" / "convert_lora_gguf.py"
        gguf_out = out_dir.with_suffix(".gguf")
        if converter.is_file():
            logger.info("orpo_role_adapter: converting PEFT -> GGUF (%s)", gguf_out)
            conv_cmd = [
                sys.executable, str(converter),
                "--peft", str(out_dir),
                "--out", str(gguf_out),
            ]
            rc2 = subprocess.call(conv_cmd, cwd=str(REPO_ROOT))
            if rc2 != 0:
                logger.error("orpo_role_adapter: GGUF conversion failed (%d)", rc2)
                return rc2
        else:
            logger.warning(
                "orpo_role_adapter: convert_lora_gguf.py missing; "
                "skipping conversion.",
            )

    logger.info(
        "orpo_role_adapter: done.  Next steps:\n"
        "  1. Verify the adapter renders sane completions vs the base.\n"
        "  2. Uncomment the matching --lora-init-without-apply line in\n"
        "     compose/llama-swap/config.yaml (or .q4_0.yaml / .q8_0.yaml).\n"
        "  3. Set settings.code_lora_enabled=true and\n"
        "     settings.code_lora_role_adapters='{\"%s\": %d, ...}'.\n"
        "  4. Restart amor-llama-swap + amor-app.\n"
        "  5. Confirm /api/code/diagnostics shows lora_attached events.",
        role, adapter_id,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
