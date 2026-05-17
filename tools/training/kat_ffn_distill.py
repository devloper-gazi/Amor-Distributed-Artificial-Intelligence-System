#!/usr/bin/env python3
"""Cycle J.2 — KAT FFN distillation with kill-switch.

Yang & Wang's Kolmogorov-Arnold Transformer (arXiv 2409.10594)
proposes replacing standard MLP FFN blocks with Group-Rational KAT
FFNs, claiming better parameter efficiency on language-modelling
benchmarks.  Plan-agent flagged this as RESEARCH-ONLY with an
8-week negative-result risk if we go directly to 7B.

**Kill-switch protocol** (this script's central contribution):

  1. Distill MLP → KAT FFN on a 1B PYTHIA variant (small, fast,
     ~2h CPU+GPU on a 4060)
  2. Measure perplexity recovery: ``kat_ppl / baseline_ppl``
  3. If recovery < 0.95 → ABORT.  Do NOT proceed to 7B.
  4. Otherwise scale to 7B (separate operator-run, deferred)

This script ships the orchestration + decision logic.  The actual
training depends on the operator-installed ``katransformer``
package (github.com/Adamdad/kat).  Without it, ``--dry-run`` and
``--simulate`` modes still exercise the kill-switch decision so the
CI guards stay green even on CPU-only hosts.

Usage::

    # Real run (operator GPU, ~2h):
    python tools/training/kat_ffn_distill.py \\
        --base pythia-1b \\
        --target-perplexity-ratio 0.95 \\
        --epochs 3 \\
        --out models/kat/pythia-1b-kat.bin

    # Smoke / CI:
    python tools/training/kat_ffn_distill.py --dry-run --base pythia-1b
    python tools/training/kat_ffn_distill.py --simulate --observed-ppl 1.04
"""

from __future__ import annotations

import argparse
import json
import logging
import math
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


# ─── Kill-switch decision ──────────────────────────────────────────


@dataclass
class KillSwitchVerdict:
    """The structured output the operator + CI both read.

    ``proceed`` is the SINGLE authoritative bit.  Everything else
    is observational + audit trail.  Plan-agent locked: a False
    verdict means do NOT scale to 7B; document as `dossier §8` vapor.
    """
    proceed: bool
    observed_perplexity_ratio: float
    target_ratio: float
    margin: float                       # observed - target (negative → abort)
    note: str = ""
    extras: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict:
        d = asdict(self)
        return d


def kill_switch_decision(
    *,
    baseline_ppl: float,
    kat_ppl: float,
    target_ratio: float = 0.95,
    safety_buffer: float = 0.0,
) -> KillSwitchVerdict:
    """Compute the kill-switch verdict.

    Convention: lower perplexity is better.  ``kat_ppl`` is the
    perplexity AFTER distillation; ``baseline_ppl`` is the MLP
    starting point.  ``target_ratio`` is the FRACTION of the
    baseline perplexity the KAT model must MATCH OR BEAT.

    Mathematically, we want ``kat_ppl / baseline_ppl ≤ 1 / target_ratio``
    (i.e. KAT recovers within `target_ratio` of baseline performance).
    Equivalently: ``baseline_ppl / kat_ppl >= target_ratio`` — the
    "recovery ratio" the literature usually reports.

    Plan-agent locked target_ratio: 0.95 — KAT must reach ≥95% of
    MLP baseline to justify the 8-week 7B scale-up.  Anything less
    is a hard ABORT.
    """
    if baseline_ppl <= 0 or kat_ppl <= 0:
        return KillSwitchVerdict(
            proceed=False,
            observed_perplexity_ratio=0.0,
            target_ratio=target_ratio,
            margin=-float("inf"),
            note=(
                f"invalid perplexity input (baseline={baseline_ppl}, "
                f"kat={kat_ppl}); refusing to scale"
            ),
        )
    # Recovery ratio: higher = better.  >=1.0 means KAT matches or
    # beats baseline.
    recovery = baseline_ppl / kat_ppl
    margin = recovery - target_ratio - safety_buffer
    proceed = margin >= 0.0
    if proceed:
        note = (
            f"recovery={recovery:.3f} ≥ target={target_ratio:.3f}"
            + (f" (+buffer {safety_buffer})" if safety_buffer else "")
            + " — kill-switch PASSED, scale to 7B"
        )
    else:
        note = (
            f"recovery={recovery:.3f} < target={target_ratio:.3f}"
            + (f" (+buffer {safety_buffer})" if safety_buffer else "")
            + " — kill-switch FAILED, ABORT.  Document as dossier §8 vapor."
        )
    return KillSwitchVerdict(
        proceed=proceed,
        observed_perplexity_ratio=recovery,
        target_ratio=target_ratio,
        margin=margin,
        note=note,
        extras={
            "baseline_perplexity": baseline_ppl,
            "kat_perplexity": kat_ppl,
            "safety_buffer": safety_buffer,
        },
    )


# ─── Training orchestrator ─────────────────────────────────────────


@dataclass
class DistillConfig:
    base_model: str
    target_perplexity_ratio: float = 0.95
    safety_buffer: float = 0.0
    epochs: int = 3
    batch_size: int = 4
    learning_rate: float = 2e-5
    eval_samples: int = 256
    output_dir: Optional[str] = None
    dataset: str = "wikitext-2-raw-v1"


def build_distill_config(args: argparse.Namespace) -> DistillConfig:
    return DistillConfig(
        base_model=args.base,
        target_perplexity_ratio=float(args.target_perplexity_ratio),
        safety_buffer=float(args.safety_buffer),
        epochs=int(args.epochs),
        batch_size=int(args.batch_size),
        learning_rate=float(args.lr),
        eval_samples=int(args.eval_samples),
        output_dir=str(args.out) if args.out else None,
        dataset=args.dataset,
    )


def run_training(cfg: DistillConfig) -> Dict[str, float]:
    """Invoke the actual katransformer training pipeline.

    Returns ``{"baseline_ppl": ..., "kat_ppl": ..., "wall_clock_s": ...}``.
    Heavy ML deps (torch, katransformer, datasets) are imported INSIDE
    this function so the kill-switch decision logic remains testable
    on CPU-only / dep-free hosts.
    """
    try:
        import torch                                  # noqa: PLC0415, F401
        from transformers import AutoTokenizer, AutoModelForCausalLM  # noqa: PLC0415, F401
    except ImportError as exc:
        raise RuntimeError(
            f"katransformer training requires torch + transformers: {exc}. "
            "Install via requirements-training.txt on the host GPU."
        ) from exc

    # NOTE: the actual TT + KAT distillation lives upstream in the
    # ``katransformer`` repo.  We deliberately don't bake a heavy
    # 300-LOC training loop into AMOR — the wrapper calls into the
    # upstream package once the operator has it pinned.  This stub
    # raises a clear ImportError so the CI path stays clean.
    try:
        import katransformer                         # noqa: PLC0415, F401
    except ImportError as exc:
        raise RuntimeError(
            f"katransformer package not installed: {exc}. "
            "git clone https://github.com/Adamdad/kat + pip install -e .  "
            "before re-running with --no-simulate."
        ) from exc

    # Operator path lives here — kept as a comment shell because the
    # actual training script is upstream-specific.  When katransformer
    # ships a stable CLI, this block invokes it directly.
    raise NotImplementedError(
        "Operator-only path: invoke katransformer's training CLI here.  "
        "See `python -m katransformer.distill --help` upstream."
    )


# ─── CLI ────────────────────────────────────────────────────────────


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--base", default="pythia-1b",
                   help="base model HF id (default pythia-1b — kill-switch tier)")
    p.add_argument("--target-perplexity-ratio", type=float, default=0.95,
                   help="kill-switch threshold (default 0.95)")
    p.add_argument("--safety-buffer", type=float, default=0.0,
                   help="additional margin above target (default 0)")
    p.add_argument("--epochs", type=int, default=3)
    p.add_argument("--batch-size", type=int, default=4)
    p.add_argument("--lr", type=float, default=2e-5)
    p.add_argument("--eval-samples", type=int, default=256)
    p.add_argument("--dataset", default="wikitext-2-raw-v1",
                   help="HF dataset id for eval (default wikitext-2-raw-v1)")
    p.add_argument("--out", default=None,
                   help="output dir for the distilled KAT adapter")
    p.add_argument("--dry-run", action="store_true",
                   help="validate config + exit without launching training")
    p.add_argument("--simulate", action="store_true",
                   help="skip real training; use --observed-ppl / --baseline-ppl "
                        "to drive the kill-switch (CI smoke + scaling sims)")
    p.add_argument("--observed-ppl", type=float, default=None,
                   help="(simulate only) the KAT-FFN perplexity measurement")
    p.add_argument("--baseline-ppl", type=float, default=1.0,
                   help="(simulate only) the MLP baseline perplexity (default 1.0)")
    p.add_argument("--out-verdict", default=None,
                   help="persist the KillSwitchVerdict JSON to this path")
    return p


def run(args: argparse.Namespace) -> int:
    cfg = build_distill_config(args)
    logger.info("KAT distill config: base=%s target_ratio=%.3f epochs=%d",
                cfg.base_model, cfg.target_perplexity_ratio, cfg.epochs)

    if args.dry_run:
        print(json.dumps(asdict(cfg), indent=2))
        return 0

    if args.simulate:
        if args.observed_ppl is None:
            logger.error("--simulate requires --observed-ppl")
            return 2
        verdict = kill_switch_decision(
            baseline_ppl=float(args.baseline_ppl),
            kat_ppl=float(args.observed_ppl),
            target_ratio=cfg.target_perplexity_ratio,
            safety_buffer=cfg.safety_buffer,
        )
    else:
        try:
            metrics = run_training(cfg)
        except Exception as exc:
            logger.error("training failed: %s", exc)
            return 2
        verdict = kill_switch_decision(
            baseline_ppl=metrics["baseline_ppl"],
            kat_ppl=metrics["kat_ppl"],
            target_ratio=cfg.target_perplexity_ratio,
            safety_buffer=cfg.safety_buffer,
        )

    payload = {
        "config": asdict(cfg),
        "verdict": verdict.to_dict(),
        "computed_at_utc": datetime.now(timezone.utc).isoformat(),
    }
    if args.out_verdict:
        Path(args.out_verdict).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out_verdict).write_text(
            json.dumps(payload, indent=2), encoding="utf-8",
        )
        logger.info("verdict persisted: %s", args.out_verdict)

    print(json.dumps(payload, indent=2))
    return 0 if verdict.proceed else 1


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    return run(build_parser().parse_args())


if __name__ == "__main__":
    raise SystemExit(main())
