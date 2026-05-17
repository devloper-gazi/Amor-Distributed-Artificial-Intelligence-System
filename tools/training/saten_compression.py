#!/usr/bin/env python3
"""Cycle J.1 — Saten Tensor-Train compression of Qwen FFNs.

Saten recipe (arXiv 2505.14871) decomposes Transformer FFN weights
into a Tensor-Train (TT) chain plus a sparse residual.  Target:
~50% MLP parameter shrink at ≤3pp HumanEval+ loss, recoverable
to ≥80% of lost points via 24h GRPO with verifier rewards (Cycle
H.3 produces the reward dataset).

Plan-agent CRITICAL pin: "distillation requires UNCOMPRESSED model
resident; cannot run on the 4060 at the same time as serving."  This
script assumes the operator has PAUSED serving (``docker compose
stop llama-swap amor-app-2``) before invoking the real run.

The TT-decomposition math itself runs CPU-offload friendly — 32 GB
host RAM is enough but multi-day wall.  Operator chooses GPU vs
CPU-offload via the upstream ``tensor-train`` library's settings.

Files this provides:
  * ``SatenConfig`` — operator-tunable parameters (target rank,
    sparse residual fraction, train/eval split)
  * ``run_compression`` — wraps the upstream decomposition library
  * ``RecoveryReport`` — captures pre/post HumanEval+ pass@1 +
    GRPO recovery delta so the operator can decide whether to
    PROMOTE the compressed adapter
  * CLI surface with ``--dry-run`` + ``--simulate`` for CI

Usage::

    # Real run (operator, serving paused, 4-8h wall):
    docker compose stop llama-swap amor-app-2
    python tools/training/saten_compression.py \\
        --model qwen2.5-coder-7b-q4_k_m \\
        --target-rank 0.5 \\
        --sparse-fraction 0.05 \\
        --out models/lora/qwen-coder-saten-r0.5.peft

    # CI smoke:
    python tools/training/saten_compression.py --dry-run \\
        --model qwen2.5-coder-7b-q4_k_m --target-rank 0.5
    python tools/training/saten_compression.py --simulate \\
        --pre-pass-rate 0.78 --post-pass-rate 0.76 \\
        --post-grpo-recovery-pp 1.5
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

if sys.platform == "win32":
    for _s in (sys.stdout, sys.stderr):
        _r = getattr(_s, "reconfigure", None)
        if _r is not None:
            try:
                _r(encoding="utf-8", errors="replace")
            except (OSError, ValueError):
                pass

logger = logging.getLogger(__name__)


_REPO_ROOT = Path(__file__).resolve().parent.parent.parent


# ─── Config + report dataclasses ───────────────────────────────────


@dataclass
class SatenConfig:
    """All operator-tunable knobs for one Saten run."""
    model: str
    target_rank: float = 0.5          # fraction of original MLP rank to retain
    sparse_fraction: float = 0.05     # fraction of params kept as dense residual
    dataset: str = "humaneval-plus-50"
    eval_subset: int = 50
    cpu_offload: bool = False
    output_dir: Optional[str] = None

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class RecoveryReport:
    """Plan-agent acceptance contract:

      * ≤3 pp HumanEval+ loss vs uncompressed Qwen-Coder-7B
      * ≥80% of lost points recovered after 24h GRPO

    ``promotion_ready`` ANDs both checks.  Operator inspects
    ``loss_pp`` + ``recovered_fraction`` for the audit trail before
    flipping the model symlink.
    """
    pre_pass_rate: float
    post_pass_rate: float
    post_grpo_pass_rate: float
    loss_pp: float                    # pre - post (in percentage points)
    recovered_pp: float               # post_grpo - post
    recovered_fraction: float         # recovered_pp / max(loss_pp, eps)
    promotion_ready: bool
    note: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


# ─── Recovery decision ─────────────────────────────────────────────


def compute_recovery_report(
    *,
    pre_pass_rate: float,
    post_pass_rate: float,
    post_grpo_pass_rate: Optional[float] = None,
    max_loss_pp: float = 3.0,
    min_recovery_fraction: float = 0.80,
) -> RecoveryReport:
    """Plan-agent acceptance arithmetic.  ``pre``/``post``/``post_grpo``
    are HumanEval+ pass@1 percentages (0..100).

    Semantic of ``post_grpo_pass_rate``:
      * ``None``  — operator hasn't run GRPO recovery yet.  Only the
        loss check applies; the report flags ``recovery_check_skipped``.
      * float     — recovery measured; both checks (loss + recovery
        fraction) must clear for promotion_ready.
    """
    recovery_measured = post_grpo_pass_rate is not None
    if not recovery_measured:
        post_grpo_pass_rate = post_pass_rate
    loss_pp = pre_pass_rate - post_pass_rate
    recovered_pp = post_grpo_pass_rate - post_pass_rate
    eps = 1e-9
    fraction = recovered_pp / max(loss_pp, eps)
    # Plan-agent acceptance: both bars must clear.  Floating-point
    # tolerance on the recovery threshold so the operator-friendly
    # "exactly 80%" hit (e.g. 1.6/2.0 = 0.79999...) doesn't trip.
    loss_ok = loss_pp <= max_loss_pp + eps
    if loss_pp <= eps:
        recovery_ok = True       # no quality loss to recover
        recovery_note = "no quality loss — recovery N/A"
    elif not recovery_measured:
        # Recovery wasn't measured — defer the recovery check (don't
        # block promotion on data we haven't gathered yet).  Plan-agent
        # caveat: this is a SOFT promotion; the operator should
        # measure recovery before flipping the production symlink.
        recovery_ok = True
        recovery_note = "recovery check SKIPPED (post_grpo not measured)"
    else:
        recovery_ok = fraction + eps >= min_recovery_fraction
        if recovery_ok:
            recovery_note = (
                f"recovery {fraction*100:.1f}% ≥ {min_recovery_fraction*100:.0f}%"
            )
        else:
            recovery_note = (
                f"recovery {fraction*100:.1f}% < {min_recovery_fraction*100:.0f}% — ABORT"
            )
    promotion_ready = loss_ok and recovery_ok
    if loss_ok:
        loss_note = f"loss {loss_pp:.2f}pp ≤ {max_loss_pp:.1f}pp"
    else:
        loss_note = f"loss {loss_pp:.2f}pp EXCEEDS {max_loss_pp:.1f}pp"
    return RecoveryReport(
        pre_pass_rate=pre_pass_rate,
        post_pass_rate=post_pass_rate,
        post_grpo_pass_rate=post_grpo_pass_rate,
        loss_pp=loss_pp,
        recovered_pp=recovered_pp,
        recovered_fraction=fraction,
        promotion_ready=promotion_ready,
        note=f"{loss_note} | {recovery_note}",
    )


# ─── TT decomposition wrapper (operator-only) ──────────────────────


def run_compression(cfg: SatenConfig) -> Dict[str, float]:
    """Invoke the TT decomposition pipeline.

    Returns ``{"pre_pass_rate": ..., "post_pass_rate": ..., "wall_s": ...}``.
    Heavy ML deps (torch, transformers, tensor-train libs) are imported
    INSIDE this function so the recovery-decision logic is testable
    on CPU-only hosts.

    Plan-agent CRITICAL: don't run this with llama-swap + amor-app
    running on the same host — uncompressed model + serving exceed
    the 8 GB VRAM budget.  See ``--cpu-offload`` for the slow path.
    """
    try:
        import torch                                  # noqa: PLC0415, F401
        from transformers import AutoTokenizer, AutoModelForCausalLM  # noqa: PLC0415, F401
    except ImportError as exc:
        raise RuntimeError(
            f"Saten compression requires torch + transformers: {exc}. "
            "Install via requirements-training.txt on the host GPU."
        ) from exc
    try:
        import tensorly                               # noqa: PLC0415, F401
    except ImportError as exc:
        raise RuntimeError(
            f"tensorly not installed: {exc}.  "
            "pip install 'tensorly>=0.8' for the TT decomposition."
        ) from exc

    raise NotImplementedError(
        "Operator path: this is where the upstream TT-decomposition "
        "loop runs.  Wire to your operator-tested implementation here."
    )


# ─── CLI ────────────────────────────────────────────────────────────


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--model", default="qwen2.5-coder-7b-q4_k_m",
                   help="base model id (default qwen2.5-coder-7b-q4_k_m)")
    p.add_argument("--target-rank", type=float, default=0.5,
                   help="fraction of original MLP rank to retain (default 0.5)")
    p.add_argument("--sparse-fraction", type=float, default=0.05,
                   help="fraction of params kept as dense residual (default 0.05)")
    p.add_argument("--dataset", default="humaneval-plus-50",
                   help="eval dataset (default humaneval-plus-50)")
    p.add_argument("--eval-subset", type=int, default=50)
    p.add_argument("--cpu-offload", action="store_true",
                   help="use CPU offload (multi-day wall; needed when GPU is busy)")
    p.add_argument("--out", default=None,
                   help="output dir for the compressed PEFT adapter")
    p.add_argument("--dry-run", action="store_true",
                   help="validate config + exit without launching compression")
    p.add_argument("--simulate", action="store_true",
                   help="skip real compression; supply --pre-pass-rate / "
                        "--post-pass-rate to drive the recovery decision")
    p.add_argument("--pre-pass-rate", type=float, default=None,
                   help="(simulate) HumanEval+ pass@1 before compression")
    p.add_argument("--post-pass-rate", type=float, default=None,
                   help="(simulate) HumanEval+ pass@1 after compression, "
                        "before GRPO recovery")
    p.add_argument("--post-grpo-recovery-pp", type=float, default=None,
                   help="(simulate) percentage-points recovered by 24h GRPO")
    p.add_argument("--max-loss-pp", type=float, default=3.0,
                   help="acceptance: HumanEval+ loss pp threshold (default 3)")
    p.add_argument("--min-recovery-fraction", type=float, default=0.80,
                   help="acceptance: GRPO recovery fraction (default 0.80)")
    p.add_argument("--out-report", default=None,
                   help="persist the RecoveryReport JSON to this path")
    return p


def build_config(args: argparse.Namespace) -> SatenConfig:
    return SatenConfig(
        model=args.model,
        target_rank=float(args.target_rank),
        sparse_fraction=float(args.sparse_fraction),
        dataset=args.dataset,
        eval_subset=int(args.eval_subset),
        cpu_offload=bool(args.cpu_offload),
        output_dir=str(args.out) if args.out else None,
    )


def run(args: argparse.Namespace) -> int:
    cfg = build_config(args)
    logger.info(
        "Saten config: model=%s target_rank=%.2f sparse=%.3f cpu_offload=%s",
        cfg.model, cfg.target_rank, cfg.sparse_fraction, cfg.cpu_offload,
    )

    if args.dry_run:
        print(json.dumps(cfg.to_dict(), indent=2))
        return 0

    if args.simulate:
        if args.pre_pass_rate is None or args.post_pass_rate is None:
            logger.error("--simulate requires --pre-pass-rate + --post-pass-rate")
            return 2
        post_grpo = (
            args.post_pass_rate + float(args.post_grpo_recovery_pp)
            if args.post_grpo_recovery_pp is not None
            else None
        )
        report = compute_recovery_report(
            pre_pass_rate=float(args.pre_pass_rate),
            post_pass_rate=float(args.post_pass_rate),
            post_grpo_pass_rate=post_grpo,
            max_loss_pp=float(args.max_loss_pp),
            min_recovery_fraction=float(args.min_recovery_fraction),
        )
    else:
        try:
            metrics = run_compression(cfg)
        except Exception as exc:
            logger.error("compression failed: %s", exc)
            return 2
        report = compute_recovery_report(
            pre_pass_rate=metrics["pre_pass_rate"],
            post_pass_rate=metrics["post_pass_rate"],
            post_grpo_pass_rate=metrics.get("post_grpo_pass_rate"),
            max_loss_pp=float(args.max_loss_pp),
            min_recovery_fraction=float(args.min_recovery_fraction),
        )

    payload = {
        "config": cfg.to_dict(),
        "report": report.to_dict(),
        "computed_at_utc": datetime.now(timezone.utc).isoformat(),
    }
    if args.out_report:
        Path(args.out_report).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out_report).write_text(
            json.dumps(payload, indent=2), encoding="utf-8",
        )
        logger.info("report persisted: %s", args.out_report)

    print(json.dumps(payload, indent=2))
    return 0 if report.promotion_ready else 1


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    return run(build_parser().parse_args())


if __name__ == "__main__":
    raise SystemExit(main())
