#!/usr/bin/env python3
"""
Cycle C Sprint 6 Day 2 — Unsloth + TRL ORPO trainer driver.

Trains a LoRA adapter on Qwen2.5-Coder-7B using the JSONL produced by
``export_pairs_jsonl.py``.  Configured exactly per the Cycle C plan
caveat (see ``CLAUDE.md``):

* ``r=8, alpha=16``, dropout 0.05, 7 target modules
* ``max_seq_length=2048``  (NOT 4096+ — VRAM linear in seq_len)
* effective batch = 4 (bs=1 × grad_accum=4)
* ``learning_rate=8e-6`` cosine, warmup 0.1
* paged adamw 8-bit, ``beta=0.1``, 1 epoch
* fp16/bf16 auto-detect

Usage
-----

    python tools/training/orpo_qwen_coder.py \\
        --jsonl /app/data/training/pairs_2026-05-04.jsonl \\
        --out  /app/models/lora/amor-orpo-2026-05-04

Outputs a PEFT adapter directory; pair with
``convert_lora_gguf.py`` to get the GGUF llama.cpp can hot-load.

This script is HEAVY — it imports unsloth + torch only inside
``main()`` so the import cost is paid only when the script is run,
not every time something opens the file in a code-search tool.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from pathlib import Path

logger = logging.getLogger(__name__)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Run ORPO fine-tuning on Qwen2.5-Coder-7B with Unsloth.",
    )
    p.add_argument("--jsonl", required=True, help="ORPO JSONL from export_pairs_jsonl.py")
    p.add_argument("--out", required=True, help="output PEFT adapter directory")
    p.add_argument(
        "--model-name",
        default="unsloth/Qwen2.5-Coder-7B-Instruct-bnb-4bit",
        help="HF model id; default is the 4-bit Unsloth release",
    )
    p.add_argument("--max-seq-length", type=int, default=2048)
    p.add_argument("--epochs", type=float, default=1.0)
    p.add_argument("--batch-size", type=int, default=1)
    p.add_argument("--grad-accum", type=int, default=4)
    p.add_argument("--lr", type=float, default=8e-6)
    p.add_argument("--lora-r", type=int, default=8)
    p.add_argument("--lora-alpha", type=int, default=16)
    p.add_argument("--lora-dropout", type=float, default=0.05)
    p.add_argument("--beta", type=float, default=0.1, help="ORPO odds-ratio weight")
    p.add_argument("--seed", type=int, default=42)
    p.add_argument("--max-prompt-length", type=int, default=512)
    p.add_argument("--logging-steps", type=int, default=1)
    p.add_argument(
        "--min-pairs",
        type=int,
        default=200,
        help="refuse to start if fewer than this many pairs in the JSONL",
    )
    p.add_argument(
        "--allow-tiny",
        action="store_true",
        help="bypass --min-pairs (useful for smoke tests)",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help=(
            "validate the JSONL + config and print the planned trainer "
            "args without importing unsloth / starting training. "
            "Lets CI exercise this script without a GPU."
        ),
    )
    # Cycle H.0.3 — opt-in GRPO trainer.  Default stays "orpo" so all
    # existing operator scripts/cron entries keep their current
    # behaviour.  When "grpo" is selected, the runner expects
    # `reward_chosen` + `reward_rejected` columns on the dataset
    # (produced by `tools/training/verifier_rewards.py:annotate_jsonl_file`).
    p.add_argument(
        "--trainer-type",
        choices=("orpo", "grpo"),
        default="orpo",
        help=(
            "training algorithm.  ORPO (default, backward-compat) uses "
            "TRL ORPOTrainer.  GRPO opts into TRL >=0.18 GRPOTrainer and "
            "reads scalar reward columns produced by the verifier_rewards "
            "annotation step.  Plan-agent pin: trl==0.18.* required."
        ),
    )
    return p


def load_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        raise FileNotFoundError(f"JSONL not found: {path}")
    rows: list[dict] = []
    with path.open("r", encoding="utf-8") as fh:
        for ln, raw in enumerate(fh, start=1):
            raw = raw.strip()
            if not raw:
                continue
            try:
                row = json.loads(raw)
            except json.JSONDecodeError as exc:
                raise ValueError(f"line {ln}: bad JSON ({exc})") from exc
            for k in ("prompt", "chosen", "rejected"):
                if k not in row:
                    raise ValueError(f"line {ln}: missing field {k!r}")
            rows.append(row)
    return rows


def build_trainer_args(args: argparse.Namespace) -> dict:
    """Return a dict the dry-run can serialise; the real training
    path passes the same shape into ``ORPOConfig``."""
    return {
        "per_device_train_batch_size": args.batch_size,
        "gradient_accumulation_steps": args.grad_accum,
        "warmup_ratio": 0.1,
        "num_train_epochs": args.epochs,
        "learning_rate": args.lr,
        "lr_scheduler_type": "cosine",
        "logging_steps": args.logging_steps,
        "optim": "paged_adamw_8bit",
        "seed": args.seed,
        "output_dir": args.out,
        "beta": args.beta,
        "max_length": args.max_seq_length,
        "max_prompt_length": args.max_prompt_length,
    }


def run(args: argparse.Namespace) -> int:
    """Body of main() — separated so tests can drive the runner with
    pre-parsed args without re-invoking the CLI."""
    rows = load_jsonl(Path(args.jsonl))
    n = len(rows)
    if n < args.min_pairs and not args.allow_tiny:
        logger.error(
            "ORPO refuses to train: %d rows in JSONL, threshold is %d. "
            "Pass --allow-tiny to bypass for smoke tests.",
            n,
            args.min_pairs,
        )
        return 2

    cfg = build_trainer_args(args)
    out_path = Path(args.out)
    out_path.mkdir(parents=True, exist_ok=True)

    if args.dry_run:
        # Persist the planned args next to the (empty) output dir so a
        # later operator can see exactly what the run would have done.
        (out_path / "planned_orpo_config.json").write_text(
            json.dumps(
                {
                    "model_name": args.model_name,
                    "max_seq_length": args.max_seq_length,
                    "lora": {
                        "r": args.lora_r,
                        "alpha": args.lora_alpha,
                        "dropout": args.lora_dropout,
                        "target_modules": [
                            "q_proj", "k_proj", "v_proj", "o_proj",
                            "gate_proj", "up_proj", "down_proj",
                        ],
                    },
                    "config": cfg,
                    "pairs": n,
                },
                indent=2,
            ),
            encoding="utf-8",
        )
        print(
            json.dumps(
                {
                    "dry_run": True,
                    "pairs": n,
                    "out": str(out_path),
                    "config": cfg,
                    # Cycle H.0.3 — surface trainer choice in the
                    # dry-run payload so the cron's verbose log
                    # records which factory branch would fire.
                    "trainer_type": getattr(args, "trainer_type", "orpo"),
                },
                indent=2,
            ),
        )
        return 0

    # ── Real training path — heavy imports live here. ───────────────
    logger.info("loading unsloth + transformers (this can take ~30 s) ...")
    trainer_type = getattr(args, "trainer_type", "orpo").lower()
    try:
        from unsloth import FastLanguageModel  # noqa: PLC0415
        import torch  # noqa: PLC0415
        from datasets import Dataset  # noqa: PLC0415
        if trainer_type == "grpo":
            # Cycle H.0.3 — Plan-agent pin: trl>=0.18 added GRPOTrainer
            # with `beta`/`reward_chosen`/`reward_rejected` semantics.
            # Earlier TRL versions don't ship the class; fail fast with
            # a clear remediation pointer.
            try:
                from unsloth import PatchGRPOTrainer  # noqa: PLC0415
                PatchGRPOTrainer()
            except ImportError:
                # Older Unsloth versions don't patch GRPO yet — the
                # base TRL trainer is still usable, just slower.
                logger.warning(
                    "unsloth.PatchGRPOTrainer unavailable — using "
                    "stock trl.GRPOTrainer (training will be slower)",
                )
            from trl import GRPOConfig, GRPOTrainer  # noqa: PLC0415
        else:
            from unsloth import PatchORPOTrainer  # noqa: PLC0415
            PatchORPOTrainer()
            from trl import ORPOConfig, ORPOTrainer  # noqa: PLC0415
    except ImportError as exc:
        if trainer_type == "grpo":
            logger.error(
                "trainer-type=grpo requires trl>=0.18 (GRPOTrainer was "
                "added in 0.18; the API drifted hard between 0.14 and "
                "0.18).  Pin requirements.txt: trl==0.18.*.  Original "
                "import error: %s", exc,
            )
        else:
            logger.error(
                "unsloth/trl not installed.  Install with `pip install "
                "\"unsloth[colab-new] @ git+https://github.com/unslothai/unsloth.git\" "
                "trl peft accelerate bitsandbytes`. (%s)",
                exc,
            )
        return 3

    logger.info("loading model %s @ max_seq_length=%d", args.model_name, args.max_seq_length)
    model, tokenizer = FastLanguageModel.from_pretrained(
        model_name=args.model_name,
        max_seq_length=args.max_seq_length,
        dtype=None,
        load_in_4bit=True,
    )
    model = FastLanguageModel.get_peft_model(
        model,
        r=args.lora_r,
        lora_alpha=args.lora_alpha,
        lora_dropout=args.lora_dropout,
        target_modules=[
            "q_proj", "k_proj", "v_proj", "o_proj",
            "gate_proj", "up_proj", "down_proj",
        ],
        bias="none",
        use_gradient_checkpointing="unsloth",
        random_state=args.seed,
        max_seq_length=args.max_seq_length,
    )

    # Cycle H.0.3 — when GRPO is selected, the dataset MUST carry
    # ``reward_chosen`` + ``reward_rejected`` scalar columns produced
    # by ``tools/training/verifier_rewards.py:annotate_jsonl_file``.
    # ORPO mode silently ignores extras (backward-compat).
    def _build_row(r: dict) -> dict:
        row = {
            "prompt": r["prompt"],
            "chosen": r["chosen"],
            "rejected": r["rejected"],
        }
        if trainer_type == "grpo":
            rc = r.get("reward_chosen")
            rr = r.get("reward_rejected")
            if rc is None or rr is None:
                raise ValueError(
                    "trainer-type=grpo requires every row to carry "
                    "`reward_chosen` + `reward_rejected` scalars; run "
                    "`tools/training/verifier_rewards.py annotate-jsonl` "
                    "first."
                )
            row["reward_chosen"] = float(rc)
            row["reward_rejected"] = float(rr)
        return row

    ds = Dataset.from_list([_build_row(r) for r in rows])

    cfg["fp16"] = not torch.cuda.is_bf16_supported()
    cfg["bf16"] = torch.cuda.is_bf16_supported()
    if trainer_type == "grpo":
        # GRPO uses scalar rewards rather than the ORPO odds-ratio
        # — `beta` keeps the same role (KL-ish weight).  Pass the
        # same cfg dict; GRPOConfig accepts the common fields.
        trainer = GRPOTrainer(
            model=model,
            tokenizer=tokenizer,
            args=GRPOConfig(**{**cfg, "beta": getattr(args, "beta", 0.1)}),
            train_dataset=ds,
        )
    else:
        trainer = ORPOTrainer(
            model=model,
            ref_model=None,
            tokenizer=tokenizer,
            args=ORPOConfig(**cfg),
            train_dataset=ds,
        )
    logger.info("training (%s) on %d pairs ...", trainer_type, n)
    trainer.train()
    logger.info("saving adapter to %s", out_path)
    trainer.save_model(str(out_path))
    print(json.dumps(
        {"trained": True, "trainer": trainer_type, "pairs": n, "out": str(out_path)},
        indent=2,
    ))
    return 0


def main() -> int:
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"))
    return run(build_parser().parse_args())


if __name__ == "__main__":
    sys.exit(main())
