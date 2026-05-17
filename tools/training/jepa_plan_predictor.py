#!/usr/bin/env python3
"""Cycle K.2 — JEPA plan-embedding predictor.

LeCun's V-JEPA 2 method (arXiv 2506.09985) adapted to AMOR's
session traces.  The original V-JEPA predicts video-clip embeddings
in latent space; we apply the same idea to PLAN-THEN-DECODE pairs:

  * Encoder ``E`` maps (prompt, code_context, plan_so_far) → vector
  * Predictor ``P`` maps E(state_t) → predicted E(state_{t+1})
  * Loss is L2 (or cosine) in the embedding space — NOT next-token
    cross-entropy (the JEPA insight: skip the autoregressive sampling
    cost during training).

At AMOR's scale (~1500 sessions in 12 months), training a tiny
embedding predictor is ~2h wall on the 4060 GPU.  At inference,
the predictor is a Layer-2 PRE-FILTER: imagine the next 1-3 planner
steps and short-circuit a full LLM call when the predicted next-
step embedding stays in a "well-trodden" region.

This script ships the orchestration + dataset preparation logic.
The actual neural-network training depends on torch + the dataset
shape AMOR exports from its session traces (Cycle H.3's preference-
pair JSONL with an extra `embedding` column is a candidate source).

Usage::

    # Real run (operator GPU, ~2h):
    python tools/training/jepa_plan_predictor.py \\
        --train data/preference_pairs/build.jsonl \\
        --out models/jepa/plan_predictor.bin

    # Smoke / CI:
    python tools/training/jepa_plan_predictor.py --dry-run \\
        --train data/preference_pairs/build.jsonl
    python tools/training/jepa_plan_predictor.py --simulate \\
        --train-loss 0.42 --val-loss 0.48 --target-loss 0.60
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
from typing import Any, Dict, List, Optional

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


# ─── Config + report ───────────────────────────────────────────────


@dataclass
class JepaTrainConfig:
    """All operator-tunable knobs for one JEPA training run."""
    train_jsonl: str
    val_split: float = 0.10           # 10% holdout for val-loss
    embedding_dim: int = 768          # matches nomic-embed default
    predictor_hidden_dim: int = 1024
    predictor_depth: int = 4          # 4 layers is the V-JEPA 2 sweet spot
    batch_size: int = 32
    epochs: int = 20
    learning_rate: float = 1e-4
    target_loss: float = 0.40         # acceptance: val-loss <= 0.40
    seed: int = 42
    output_dir: Optional[str] = None

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class JepaTrainReport:
    """Acceptance contract for the K.2 JEPA predictor.

    Plan-agent locked: ``val_loss`` is the gate.  Training loss can
    drop arbitrarily low (overfitting); we promote ONLY when the
    held-out val_loss is below ``target_loss``.
    """
    train_loss: float
    val_loss: float
    target_loss: float
    epochs_completed: int
    promotion_ready: bool
    note: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


# ─── Dataset prep ──────────────────────────────────────────────────


def load_plan_pairs(jsonl_path: Path) -> List[Dict[str, Any]]:
    """Read a JSONL of preference pairs (Cycle H.3 shape) and return
    the ones that have embedding columns ready for JEPA training.

    Expected per-row keys (best-effort, robust to absence):
      * ``prompt`` (str)
      * ``chosen`` (str) — the higher-quality completion
      * ``chosen_embedding`` (list[float]) — pre-embedded chosen
      * ``rejected`` / ``rejected_embedding`` — same shape

    Rows without embeddings are skipped (the operator can run an
    embed-only pre-processing step to populate them).
    """
    if not jsonl_path.is_file():
        raise FileNotFoundError(f"plan-pairs JSONL not found: {jsonl_path}")
    rows: List[Dict[str, Any]] = []
    for raw in jsonl_path.read_text(encoding="utf-8").splitlines():
        raw = raw.strip()
        if not raw:
            continue
        try:
            row = json.loads(raw)
        except json.JSONDecodeError:
            continue
        # Require at least one embedding column to be present.
        if not (row.get("chosen_embedding") or row.get("rejected_embedding")):
            continue
        rows.append(row)
    return rows


def split_train_val(rows: List[Dict[str, Any]], *,
                    val_split: float = 0.10, seed: int = 42,
                    ) -> tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """Deterministic train/val split.  ``seed`` keeps re-runs
    comparable; the operator passes the same seed when comparing
    epochs over many runs."""
    import random
    rng = random.Random(seed)
    shuffled = list(rows)
    rng.shuffle(shuffled)
    cut = int(round(len(shuffled) * (1.0 - val_split)))
    return shuffled[:cut], shuffled[cut:]


# ─── Acceptance decision ───────────────────────────────────────────


def compute_jepa_report(
    *,
    train_loss: float,
    val_loss: float,
    target_loss: float,
    epochs_completed: int,
) -> JepaTrainReport:
    """Plan-agent locked: promote only when val_loss ≤ target_loss
    AND val_loss is within 50% of train_loss (avoid extreme
    overfitting).  Both gates must clear."""
    eps = 1e-9
    loss_ok = val_loss <= target_loss + eps
    if train_loss <= eps:
        overfit_check_ok = (val_loss <= target_loss + eps)
    else:
        ratio = val_loss / train_loss
        overfit_check_ok = ratio <= 1.5      # val no more than 50% above train
    promotion_ready = loss_ok and overfit_check_ok
    note_parts: list[str] = []
    if loss_ok:
        note_parts.append(f"val_loss={val_loss:.3f} ≤ target={target_loss:.3f}")
    else:
        note_parts.append(
            f"val_loss={val_loss:.3f} EXCEEDS target={target_loss:.3f}"
        )
    if overfit_check_ok:
        note_parts.append("no extreme overfitting detected")
    else:
        note_parts.append(
            f"val/train ratio={(val_loss / max(train_loss, eps)):.2f} > 1.50 — "
            "overfit ABORT"
        )
    return JepaTrainReport(
        train_loss=train_loss,
        val_loss=val_loss,
        target_loss=target_loss,
        epochs_completed=epochs_completed,
        promotion_ready=promotion_ready,
        note=" | ".join(note_parts),
    )


# ─── Training wrapper (operator-only) ──────────────────────────────


def run_training(cfg: JepaTrainConfig) -> Dict[str, float]:
    """Invoke the actual V-JEPA 2 training pipeline.

    Returns ``{"train_loss": ..., "val_loss": ..., "epochs": ...}``.
    Heavy ML deps (torch) are imported INSIDE this function so the
    acceptance-decision logic remains testable on CPU-only hosts.
    """
    try:
        import torch                                  # noqa: PLC0415, F401
        import numpy as np                            # noqa: PLC0415, F401
    except ImportError as exc:
        raise RuntimeError(
            f"V-JEPA predictor training requires torch + numpy: {exc}. "
            "Install via requirements-training.txt on the host GPU."
        ) from exc

    train_rows = load_plan_pairs(Path(cfg.train_jsonl))
    if not train_rows:
        raise RuntimeError(
            f"no rows with embeddings in {cfg.train_jsonl}.  Run the "
            "embed-only preprocessor first (see docs/jepa_dataset.md)."
        )

    # The actual neural network is a thin MLP predictor — operator
    # implementation lives in the V-JEPA 2 fork.  Wire to it here.
    raise NotImplementedError(
        "Operator path: invoke the V-JEPA 2 training script here "
        "with the loaded train_rows + cfg fields."
    )


# ─── CLI ────────────────────────────────────────────────────────────


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--train", required=False, default=None,
                   help="preference-pair JSONL with embedding columns")
    p.add_argument("--val-split", type=float, default=0.10)
    p.add_argument("--embedding-dim", type=int, default=768)
    p.add_argument("--predictor-hidden-dim", type=int, default=1024)
    p.add_argument("--predictor-depth", type=int, default=4)
    p.add_argument("--batch-size", type=int, default=32)
    p.add_argument("--epochs", type=int, default=20)
    p.add_argument("--lr", type=float, default=1e-4)
    p.add_argument("--target-loss", type=float, default=0.40)
    p.add_argument("--seed", type=int, default=42)
    p.add_argument("--out", default=None,
                   help="output dir for the trained predictor checkpoint")
    p.add_argument("--dry-run", action="store_true",
                   help="validate config + exit without launching training")
    p.add_argument("--simulate", action="store_true",
                   help="skip real training; supply --train-loss / --val-loss "
                        "to drive the acceptance decision")
    p.add_argument("--train-loss", type=float, default=None,
                   help="(simulate) final train_loss")
    p.add_argument("--val-loss", type=float, default=None,
                   help="(simulate) final val_loss")
    p.add_argument("--epochs-completed", type=int, default=20,
                   help="(simulate) epochs that ran before stop / convergence")
    p.add_argument("--out-report", default=None,
                   help="persist the JepaTrainReport JSON to this path")
    return p


def build_config(args: argparse.Namespace) -> JepaTrainConfig:
    return JepaTrainConfig(
        train_jsonl=args.train or "",
        val_split=float(args.val_split),
        embedding_dim=int(args.embedding_dim),
        predictor_hidden_dim=int(args.predictor_hidden_dim),
        predictor_depth=int(args.predictor_depth),
        batch_size=int(args.batch_size),
        epochs=int(args.epochs),
        learning_rate=float(args.lr),
        target_loss=float(args.target_loss),
        seed=int(args.seed),
        output_dir=str(args.out) if args.out else None,
    )


def run(args: argparse.Namespace) -> int:
    cfg = build_config(args)
    logger.info(
        "JEPA config: embedding_dim=%d hidden=%d depth=%d epochs=%d target_loss=%.3f",
        cfg.embedding_dim, cfg.predictor_hidden_dim, cfg.predictor_depth,
        cfg.epochs, cfg.target_loss,
    )

    if args.dry_run:
        print(json.dumps(cfg.to_dict(), indent=2))
        return 0

    if args.simulate:
        if args.train_loss is None or args.val_loss is None:
            logger.error("--simulate requires --train-loss + --val-loss")
            return 2
        report = compute_jepa_report(
            train_loss=float(args.train_loss),
            val_loss=float(args.val_loss),
            target_loss=cfg.target_loss,
            epochs_completed=int(args.epochs_completed),
        )
    else:
        if not args.train:
            logger.error("--train <jsonl> required for the real path")
            return 2
        try:
            metrics = run_training(cfg)
        except Exception as exc:
            logger.error("training failed: %s", exc)
            return 2
        report = compute_jepa_report(
            train_loss=metrics["train_loss"],
            val_loss=metrics["val_loss"],
            target_loss=cfg.target_loss,
            epochs_completed=int(metrics.get("epochs", cfg.epochs)),
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
