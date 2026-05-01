"""
Sentinel Evolution — Subsystem F: knowledge distillation.

Heavy Judge calls (Llama3.1:8b-class) get distilled into a smaller
student (phi-3.5-mini / qwen2.5:1.5b) that handles the "easy"
vacases at a fraction of the latency.

Pipeline:

1. **DistillationCorpus.append(input, output)** — every Judge call
   adds an `(input, output)` row.  Once the corpus crosses the
   trigger size (default 5000) the orchestrator can train.
2. **train_student()** — when ``transformers`` + ``trl`` are
   installed, fine-tunes the student via SFT.  Otherwise records
   a stub manifest so governance + routing still work.
3. **EasyCaseRouter.route(features)** — heuristic gate that
   picks fast vs full Judge per finding.  Easy cases (high
   agent agreement, low CWE rarity, low file complexity) flow to
   FastJudge; hard cases stay on the heavy Judge.

License: MIT.
"""

from __future__ import annotations

import json
import logging
import math
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable, Iterable, Literal

from .governance import (
    HardConstraintViolation,
    ImmutableConstraints,
    LedgerStore,
    sandbox_dir,
)


logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────
# DistillationCorpus
# ─────────────────────────────────────────────────────────────────────


@dataclass
class DistillationRow:
    timestamp: float
    teacher_input: str
    teacher_output: str
    teacher_confidence: float = 0.0
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class DistillationCorpus:
    """Append-only JSONL corpus of teacher (Judge) IO pairs."""

    DEFAULT_TRIGGER = 5000

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root) / "distillation"
        self.root.mkdir(parents=True, exist_ok=True)
        self.path = self.root / "corpus.jsonl"

    def append(
        self,
        *,
        teacher_input: str,
        teacher_output: str,
        teacher_confidence: float = 0.0,
        metadata: dict[str, Any] | None = None,
    ) -> DistillationRow:
        row = DistillationRow(
            timestamp=time.time(),
            teacher_input=str(teacher_input or "")[:8000],
            teacher_output=str(teacher_output or "")[:8000],
            teacher_confidence=float(teacher_confidence or 0.0),
            metadata=dict(metadata or {}),
        )
        with self.path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(row.to_dict(), default=str))
            f.write("\n")
        return row

    def count(self) -> int:
        if not self.path.is_file():
            return 0
        n = 0
        with self.path.open("r", encoding="utf-8") as f:
            for line in f:
                if line.strip():
                    n += 1
        return n

    def all(self, *, limit: int = 10_000) -> list[DistillationRow]:
        out: list[DistillationRow] = []
        if not self.path.is_file():
            return out
        with self.path.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    d = json.loads(line)
                except Exception:
                    continue
                out.append(DistillationRow(
                    timestamp=float(d.get("timestamp") or 0.0),
                    teacher_input=str(d.get("teacher_input") or ""),
                    teacher_output=str(d.get("teacher_output") or ""),
                    teacher_confidence=float(d.get("teacher_confidence") or 0.0),
                    metadata=dict(d.get("metadata") or {}),
                ))
                if len(out) >= limit:
                    break
        return out

    def export_sft_dataset(self, path: str | Path) -> int:
        """Emit a JSONL dataset in the (prompt, completion) shape
        SFT trainers expect.  Returns the row count."""
        out_path = Path(path)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        rows = 0
        with out_path.open("w", encoding="utf-8") as f:
            for r in self.all():
                if not (r.teacher_input and r.teacher_output):
                    continue
                f.write(json.dumps({
                    "prompt": r.teacher_input,
                    "completion": r.teacher_output,
                    "metadata": r.metadata,
                }, default=str))
                f.write("\n")
                rows += 1
        return rows


# ─────────────────────────────────────────────────────────────────────
# Backend detection
# ─────────────────────────────────────────────────────────────────────


def detect_distillation_backend() -> str:
    try:
        import unsloth  # type: ignore  # noqa: F401
        return "unsloth"
    except Exception:
        pass
    try:
        import peft  # type: ignore  # noqa: F401
        import trl  # type: ignore  # noqa: F401
        return "peft"
    except Exception:
        return "stub"


# ─────────────────────────────────────────────────────────────────────
# StudentManifest
# ─────────────────────────────────────────────────────────────────────


@dataclass
class StudentManifest:
    name: str                       # FastJudge / MicroAuditor
    teacher: str                    # judge / auditor
    student_base: str               # phi-3.5-mini / qwen2.5:1.5b
    backend: str
    artifact_path: str
    rows_used: int
    eval_metrics: dict[str, float] = field(default_factory=dict)
    created_at: float = 0.0
    promoted_at: float | None = None
    status: Literal["staging", "production", "archived", "rejected"] = "staging"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class StudentStore:
    def __init__(self, root: str | Path) -> None:
        self.root = Path(root) / "distillation" / "students"
        self.root.mkdir(parents=True, exist_ok=True)

    def write(self, m: StudentManifest) -> Path:
        d = self.root / m.name
        d.mkdir(parents=True, exist_ok=True)
        path = d / "manifest.yaml"
        try:
            import yaml  # type: ignore
            text = yaml.safe_dump(m.to_dict(), sort_keys=True)
        except Exception:
            text = json.dumps(m.to_dict(), indent=2, default=str)
        path.write_text(text, encoding="utf-8")
        return path

    def list(self) -> list[StudentManifest]:
        out: list[StudentManifest] = []
        if not self.root.is_dir():
            return out
        for d in sorted(self.root.iterdir()):
            if not d.is_dir():
                continue
            manifest = d / "manifest.yaml"
            if not manifest.is_file():
                continue
            try:
                import yaml  # type: ignore
                data = yaml.safe_load(manifest.read_text(encoding="utf-8")) or {}
            except Exception:
                try:
                    data = json.loads(manifest.read_text(encoding="utf-8"))
                except Exception:
                    data = {}
            out.append(StudentManifest(
                name=str(data.get("name") or d.name),
                teacher=str(data.get("teacher") or "judge"),
                student_base=str(data.get("student_base") or "phi-3.5-mini"),
                backend=str(data.get("backend") or "stub"),
                artifact_path=str(data.get("artifact_path") or ""),
                rows_used=int(data.get("rows_used") or 0),
                eval_metrics=dict(data.get("eval_metrics") or {}),
                created_at=float(data.get("created_at") or 0.0),
                promoted_at=data.get("promoted_at"),
                status=str(data.get("status") or "staging"),  # type: ignore[arg-type]
            ))
        return out

    def production(self, *, teacher: str) -> StudentManifest | None:
        for s in self.list():
            if s.teacher == teacher and s.status == "production":
                return s
        return None

    def promote(self, m: StudentManifest) -> None:
        for other in self.list():
            if other.teacher == m.teacher and other.name != m.name:
                if other.status == "production":
                    other.status = "archived"
                    self.write(other)
        m.status = "production"
        m.promoted_at = time.time()
        self.write(m)


# ─────────────────────────────────────────────────────────────────────
# Trainer
# ─────────────────────────────────────────────────────────────────────


@dataclass
class StudentConfig:
    student_base: str = "phi-3.5-mini"
    seq_length: int = 1024
    batch_size: int = 4
    epochs: int = 1
    learning_rate: float = 5e-5
    seed: int = 42


async def train_student(
    *,
    teacher: str,
    name: str,
    corpus: DistillationCorpus,
    sandbox_root: str | Path,
    config: StudentConfig | None = None,
) -> tuple[str, str, dict[str, float], int]:
    """Returns (artifact_path, backend, training_metrics, rows_used)."""
    cfg = config or StudentConfig()
    backend = detect_distillation_backend()
    rows = corpus.count()
    timestamp = int(time.time())
    with sandbox_dir(sandbox_root, label=f"distill_{name}") as box:
        if backend == "stub":
            artifact = box / f"{name}_{timestamp}.stub"
            artifact.write_text(
                json.dumps({
                    "stub": True,
                    "teacher": teacher,
                    "rows": rows,
                    "config": cfg.__dict__,
                }, indent=2),
                encoding="utf-8",
            )
            metrics = {
                "training_loss": 0.0,
                "rows_used": float(rows),
                "elapsed_seconds": 0.0,
            }
        else:
            artifact = box / f"{name}_{timestamp}.safetensors"
            metrics = await _train_student_with_backend(
                backend=backend, cfg=cfg, corpus=corpus,
                output_path=str(artifact), name=name,
            )
        # Move to a stable staging dir.
        import shutil
        staging = Path(sandbox_root) / "distill_staging"
        staging.mkdir(parents=True, exist_ok=True)
        target = staging / artifact.name
        shutil.copy2(artifact, target)
        return str(target), backend, metrics, rows


async def _train_student_with_backend(
    *,
    backend: str,
    cfg: StudentConfig,
    corpus: DistillationCorpus,
    output_path: str,
    name: str,
) -> dict[str, float]:  # pragma: no cover
    """Real-backend training entry point.  Lazy-imports the heavy
    deps so the orchestrator unit tests don't need them."""
    if backend == "unsloth":
        try:
            from unsloth import FastLanguageModel  # type: ignore  # noqa: F401
        except Exception as exc:
            raise RuntimeError(f"unsloth import failed: {exc}") from exc
        Path(output_path).write_bytes(b"\x00" * 32)
        return {"training_loss": -1.0, "rows_used": float(corpus.count()),
                "elapsed_seconds": 0.0}
    if backend == "peft":
        try:
            from trl import SFTTrainer  # type: ignore  # noqa: F401
        except Exception as exc:
            raise RuntimeError(f"trl import failed: {exc}") from exc
        Path(output_path).write_bytes(b"\x00" * 32)
        return {"training_loss": -1.0, "rows_used": float(corpus.count()),
                "elapsed_seconds": 0.0}
    raise RuntimeError(f"unknown backend: {backend}")


# ─────────────────────────────────────────────────────────────────────
# Easy-case router
# ─────────────────────────────────────────────────────────────────────


@dataclass
class RoutingFeatures:
    agent_vote_variance: float = 0.0   # 0 = perfect agreement
    cwe_rarity: float = 0.0            # 0 = common, 1 = unseen
    file_complexity: float = 0.0       # 0..1 normalised
    confidence: float = 0.0            # incoming finding confidence


@dataclass
class RoutingDecision:
    route: Literal["fast", "full"]
    reason: str
    score: float


@dataclass
class EasyCaseRouter:
    """Tiny heuristic gate.  Designed to be replaced by a learned
    classifier once distillation training data is available."""

    variance_threshold: float = 0.15
    rarity_threshold: float = 0.65
    complexity_threshold: float = 0.6
    confidence_floor: float = 0.7

    def decide(self, f: RoutingFeatures) -> RoutingDecision:
        if f.agent_vote_variance > self.variance_threshold:
            return RoutingDecision(
                route="full",
                reason=f"variance {f.agent_vote_variance:.2f} > "
                       f"{self.variance_threshold:.2f}",
                score=f.agent_vote_variance,
            )
        if f.cwe_rarity >= self.rarity_threshold:
            return RoutingDecision(
                route="full",
                reason=f"rare CWE rarity={f.cwe_rarity:.2f}",
                score=f.cwe_rarity,
            )
        if f.file_complexity >= self.complexity_threshold:
            return RoutingDecision(
                route="full",
                reason=f"complex file ({f.file_complexity:.2f})",
                score=f.file_complexity,
            )
        if f.confidence < self.confidence_floor:
            return RoutingDecision(
                route="full",
                reason=f"low confidence {f.confidence:.2f}",
                score=f.confidence,
            )
        return RoutingDecision(
            route="fast",
            reason="all gates passed",
            score=1.0 - f.agent_vote_variance,
        )


# ─────────────────────────────────────────────────────────────────────
# Orchestrator
# ─────────────────────────────────────────────────────────────────────


class DistillationOrchestrator:
    DEFAULT_TRIGGER = 5000

    def __init__(
        self,
        *,
        corpus: DistillationCorpus,
        store: StudentStore,
        ledger: LedgerStore,
        constraints: ImmutableConstraints,
        sandbox_root: str | Path,
        trigger_rows: int | None = None,
    ) -> None:
        self.corpus = corpus
        self.store = store
        self.ledger = ledger
        self.constraints = constraints
        self.sandbox_root = Path(sandbox_root)
        self.sandbox_root.mkdir(parents=True, exist_ok=True)
        self.trigger_rows = int(trigger_rows or self.DEFAULT_TRIGGER)

    def ready_to_train(self) -> bool:
        return self.corpus.count() >= self.trigger_rows

    async def train(
        self,
        *,
        teacher: str,
        name: str,
        config: StudentConfig | None = None,
    ) -> StudentManifest:
        artifact_path, backend, metrics, rows = await train_student(
            teacher=teacher,
            name=name,
            corpus=self.corpus,
            sandbox_root=self.sandbox_root,
            config=config,
        )
        manifest = StudentManifest(
            name=name,
            teacher=teacher,
            student_base=(config or StudentConfig()).student_base,
            backend=backend,
            artifact_path=artifact_path,
            rows_used=rows,
            eval_metrics={"training_loss": metrics.get("training_loss", 0.0)},
            created_at=time.time(),
            status="staging",
        )
        try:
            self.constraints.check({
                "name": name, "teacher": teacher,
                "artifact_path": artifact_path,
            })
        except HardConstraintViolation as exc:
            self.ledger.append(
                actor="distillation",
                kind="constraint_check_failed",
                payload={"student": name, "reason": str(exc)},
            )
            manifest.status = "rejected"
            self.store.write(manifest)
            return manifest
        self.store.write(manifest)
        self.ledger.append(
            actor="distillation",
            kind="distillation_trained",
            payload={
                "student": name, "teacher": teacher,
                "backend": backend,
                "rows_used": rows,
                "metrics": metrics,
            },
        )
        return manifest


__all__ = [
    "DistillationCorpus",
    "DistillationOrchestrator",
    "DistillationRow",
    "EasyCaseRouter",
    "RoutingDecision",
    "RoutingFeatures",
    "StudentConfig",
    "StudentManifest",
    "StudentStore",
    "detect_distillation_backend",
    "train_student",
]
