"""
QuickCode V2 — typed contracts (Pydantic v2 IR).

These are the data structures shared between the new V2 modules
(``router``, ``striatum``, ``parsel``, ``sk_coder``, ``symcode``,
``mcts``, ``seeker``, ``anton_brain``, ``preferences``,
``sandbox_tier``).

Why a separate file?  ``models.py`` ships the legacy
``QuickCodeRequest`` / ``QuickCodeBundle`` dataclasses that already
serialise across HTTP/SSE/Mongo and that the rest of AMOR depends on.
We do not change those.  The new V2 layer needs richer typed records
(decomposition graphs, MCTS nodes, contract conditions, preference
pairs) that benefit from Pydantic's runtime validation, so we keep
the two sides clearly separated:

* ``models.py``   — public dataclass surface (stable, JSON-cleanable
                    with ``asdict``)
* ``contracts.py`` — internal typed IR (Pydantic v2, ``extra="forbid"``,
                     ``frozen=True`` where the record is produced once)

Design notes
------------

* Pydantic v2 — same version pinned in ``requirements.txt``
  (``pydantic==2.5.3``).
* ``model_config = ConfigDict(extra="forbid")`` everywhere so that
  drift between modules is caught at the boundary, not deep inside
  the engine.
* No content filters / refusal language anywhere in this file or any
  consumer.  The user explicitly asked for that.  Infrastructure
  security (prompt injection / shell injection / secret leakage) is
  handled by ``code_intelligence/adversarial_reviewer.py``, not here.
"""

from __future__ import annotations

import time
from enum import Enum
from typing import Any, Literal, TYPE_CHECKING

from pydantic import BaseModel, ConfigDict, Field, field_validator

if TYPE_CHECKING:
    # Only used by the ``TaskIR.from_quick_code_request`` adapter; pulling
    # it at runtime would create a hard import cycle once ``engine.py``
    # imports both files.
    from .models import QuickCodeRequest


# ─────────────────────────────────────────────────────────────────────
# Enums
# ─────────────────────────────────────────────────────────────────────


class TaskComplexity(str, Enum):
    """Task buckets emitted by ``router.TaskClassifier``."""

    TRIVIAL = "trivial"
    SIMPLE = "simple"
    COMPLEX = "complex"
    MATH = "math"

    @classmethod
    def coerce(cls, value: Any) -> "TaskComplexity | None":
        """Soft cast — returns ``None`` on unknown input rather than
        raising. Used by ``QuickCodeRequest.normalize`` so a stray
        client value never aborts the request."""
        if value is None:
            return None
        try:
            return cls(str(value).strip().lower())
        except (ValueError, TypeError):
            return None


class SandboxTier(str, Enum):
    """Resource-budget tier picked by ``sandbox_tier.SandboxTier``."""

    QUICK = "quick"  # 256 MB / 15 s — fast iteration
    PRO = "pro"      # 512 MB / 45 s — Pro mode

    @classmethod
    def coerce(cls, value: Any) -> "SandboxTier":
        try:
            return cls(str(value).strip().lower())
        except (ValueError, TypeError):
            return cls.QUICK


# ─────────────────────────────────────────────────────────────────────
# Design-by-Contract sub-task graph
# ─────────────────────────────────────────────────────────────────────


class ContractCondition(BaseModel):
    """A pre- or post-condition on a sub-task."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: Literal["pre", "post"]
    expression: str = Field(min_length=1, max_length=2000)
    description: str = Field(default="", max_length=2000)


class SubTask(BaseModel):
    """One node in a Parsel-style decomposition graph."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1, max_length=200)
    title: str = Field(min_length=1, max_length=400)
    description: str = Field(default="", max_length=4000)
    contract_pre: list[ContractCondition] = Field(default_factory=list)
    contract_post: list[ContractCondition] = Field(default_factory=list)
    dependencies: list[str] = Field(default_factory=list)

    @field_validator("dependencies")
    @classmethod
    def _no_self_dep(cls, v: list[str], info: Any) -> list[str]:
        # Pydantic v2 validators see field-level data, not the whole
        # model — so we skip the self-cycle check here and re-run it
        # at the TaskIR level (which has all sub-task IDs in scope).
        return v


class TaskIR(BaseModel):
    """The intermediate representation a V2 phase consumes."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1, max_length=200)
    prompt: str = Field(min_length=1)
    language: str | None = None
    complexity: TaskComplexity | None = None
    subtasks: list[SubTask] = Field(default_factory=list)
    mode: SandboxTier = SandboxTier.QUICK
    code_context: str | None = None
    triage: dict[str, Any] = Field(default_factory=dict)
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("subtasks")
    @classmethod
    def _check_dep_ids(cls, v: list[SubTask]) -> list[SubTask]:
        seen: set[str] = set()
        for st in v:
            if st.id in seen:
                raise ValueError(f"duplicate sub-task id {st.id!r}")
            seen.add(st.id)
        # Now confirm every dependency points at a known id.
        ids = {st.id for st in v}
        for st in v:
            for dep in st.dependencies:
                if dep not in ids:
                    raise ValueError(
                        f"sub-task {st.id!r} depends on unknown id {dep!r}"
                    )
                if dep == st.id:
                    raise ValueError(f"sub-task {st.id!r} depends on itself")
        return v

    @classmethod
    def from_quick_code_request(
        cls,
        req: "QuickCodeRequest",
        *,
        ir_id: str,
        complexity: TaskComplexity | None = None,
        triage: dict[str, Any] | None = None,
    ) -> "TaskIR":
        """Bridge ``models.QuickCodeRequest`` (legacy dataclass) into
        the new IR.  Imported lazily to keep this module import-cycle
        free."""
        mode = (
            SandboxTier.PRO
            if str(getattr(req, "mode", "quick")).lower() == "pro"
            else SandboxTier.QUICK
        )
        hint = TaskComplexity.coerce(getattr(req, "complexity_hint", None))
        return cls(
            id=ir_id,
            prompt=req.prompt,
            language=req.language,
            complexity=complexity or hint,
            subtasks=[],
            mode=mode,
            code_context=req.code_context,
            triage=dict(triage or {}),
            metadata={
                "effort": getattr(req, "effort", "medium"),
                "use_mesh": bool(getattr(req, "use_mesh", True)),
                "max_refine": int(getattr(req, "max_refine", 2) or 0),
            },
        )


# ─────────────────────────────────────────────────────────────────────
# Execution + verification records
# ─────────────────────────────────────────────────────────────────────


class TestResult(BaseModel):
    """One unit-test outcome.  Produced by ``sandbox_tier`` /
    ``seeker``.  Fields chosen to match the legacy
    ``QuickCodeVerification.execution`` dict so it serialises cleanly
    into the existing bundle."""

    # Tells pytest "this is a Pydantic model, not a test class" so
    # the harmless PytestCollectionWarning never fires.
    __test__ = False

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str = Field(min_length=1, max_length=400)
    passed: bool
    duration_ms: float = Field(ge=0.0)
    error: str | None = None


class SandboxResult(BaseModel):
    """One sandbox-execution outcome.  Includes resource accounting."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    ok: bool
    stdout: str = ""
    stderr: str = ""
    exit_code: int = 0
    duration_ms: float = Field(ge=0.0, default=0.0)
    memory_mb: float = Field(ge=0.0, default=0.0)
    timed_out: bool = False
    tier: SandboxTier = SandboxTier.QUICK
    tests: list[TestResult] = Field(default_factory=list)

    @classmethod
    def from_dict(cls, d: dict[str, Any] | None) -> "SandboxResult":
        if not d:
            return cls(ok=False)
        # Resilient mapping — the legacy execution dict has a few
        # different key shapes depending on which sandbox produced it.
        ok = bool(
            d.get("ok")
            or d.get("passed")
            or (d.get("exit_code", 1) == 0 and not d.get("error"))
        )
        return cls(
            ok=ok,
            stdout=str(d.get("stdout") or "")[:8000],
            stderr=str(d.get("stderr") or "")[:8000],
            exit_code=int(d.get("exit_code") or 0),
            duration_ms=float(d.get("duration_ms") or 0.0),
            memory_mb=float(d.get("memory_mb") or 0.0),
            timed_out=bool(d.get("timed_out") or False),
            tier=SandboxTier.coerce(d.get("tier") or "quick"),
        )


class SymValidationResult(BaseModel):
    """SymPy equivalence-check verdict (``symcode.SymCode``)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    ok: bool
    iterations: int = Field(ge=0, le=3, default=0)
    equivalence_class: str = ""
    rationale: str = ""
    error: str | None = None


# ─────────────────────────────────────────────────────────────────────
# Retrieval + tournament records
# ─────────────────────────────────────────────────────────────────────


class CodeSnippet(BaseModel):
    """One BM25/cosine-retrieved snippet."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    source: str = Field(min_length=1)
    score: float = Field(ge=0.0)
    source_path: str = ""
    language: str = "python"
    bm25_score: float = Field(ge=0.0, default=0.0)
    cosine_score: float = Field(ge=0.0, default=0.0)


class MCTSNode(BaseModel):
    """One node in the MCTS tree (``mcts.MCTSRunner``)."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1)
    parent_id: str | None = None
    code: str = ""
    score: float = 0.0
    visit_count: int = Field(ge=0, default=0)
    depth: int = Field(ge=0, default=0)
    children: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)


# ─────────────────────────────────────────────────────────────────────
# Preference + telemetry
# ─────────────────────────────────────────────────────────────────────


class PreferencePair(BaseModel):
    """ORPO-style preference pair (chosen vs. rejected) emitted by
    ``preferences.ORPOExporter`` once per refine cycle."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    prompt: str = Field(min_length=1)
    chosen: str = Field(min_length=1)
    rejected: str = Field(min_length=1)
    reward_delta: float = 0.0
    metadata: dict[str, Any] = Field(default_factory=dict)


class SSEEvent(BaseModel):
    """Server-Sent-Events envelope.  Mirrors the dict shape that
    ``quick_code_routes.py`` already emits."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    type: str = Field(min_length=1, max_length=200)
    payload: dict[str, Any] = Field(default_factory=dict)
    timestamp: float = Field(default_factory=lambda: time.time())
    session_id: str | None = None


# ─────────────────────────────────────────────────────────────────────
# Public API
# ─────────────────────────────────────────────────────────────────────


__all__ = [
    "TaskComplexity",
    "SandboxTier",
    "ContractCondition",
    "SubTask",
    "TaskIR",
    "TestResult",
    "SandboxResult",
    "SymValidationResult",
    "CodeSnippet",
    "MCTSNode",
    "PreferencePair",
    "SSEEvent",
]
