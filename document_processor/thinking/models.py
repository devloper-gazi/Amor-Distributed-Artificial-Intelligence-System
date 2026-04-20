"""
Thinking Mode — request / response / session models.

Keep these deliberately small and JSON-friendly: every field that appears
here is either sent over the wire to the UI or persisted into the session
snapshot (and replayed back to late SSE subscribers).
"""

from __future__ import annotations

from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# /analyze — "should we ask clarifying questions?"
# ---------------------------------------------------------------------------

Complexity = Literal["trivial", "moderate", "complex", "expert"]
DeliverableKind = Literal[
    "auto",
    "plan",
    "architecture",
    "code",
    "analysis",
    "decision",
    "explanation",
]


class ClarifyingQuestion(BaseModel):
    """A single targeted question we want the user to answer before thinking."""

    id: str = Field(..., description="Stable identifier so answers can be keyed back")
    question: str = Field(..., description="The question shown to the user")
    why_it_matters: str = Field(
        ...,
        description=(
            "1-sentence rationale shown under the question so the user "
            "understands the impact of their answer"
        ),
    )
    suggestions: List[str] = Field(
        default_factory=list,
        description="Quick-pick chips offered alongside the free-text box",
    )
    input_type: Literal["text", "choice", "number", "multiline"] = "text"
    placeholder: Optional[str] = None
    required: bool = False


class AnalyzeRequest(BaseModel):
    prompt: str = Field(..., min_length=1)
    deliverable: DeliverableKind = "auto"


class AnalyzeResponse(BaseModel):
    needs_clarification: bool
    complexity: Complexity
    rationale: str = Field(
        ...,
        description=(
            "One-sentence explanation of why we decided to ask (or skip) "
            "clarifying questions. Shown to the user."
        ),
    )
    detected_deliverable: DeliverableKind
    questions: List[ClarifyingQuestion] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# /think — "run the pipeline with the user's answers"
# ---------------------------------------------------------------------------


class ThinkRequest(BaseModel):
    prompt: str = Field(..., min_length=1)
    clarifications: Dict[str, str] = Field(
        default_factory=dict,
        description=(
            "Map of ClarifyingQuestion.id → user's free-text answer. Missing "
            "entries are treated as 'no answer given' and fed into the prompt "
            "as such."
        ),
    )
    # Optional hint from the /analyze response. Mostly useful for telemetry
    # — the engine re-derives this from the prompt regardless.
    detected_deliverable: DeliverableKind = "auto"
    # If the user toggled Claude mode on the UI, the route layer forwards it
    # here so the engine can pick the right LLM backend.
    provider: Literal["local", "claude"] = "local"
    # Let the UI dial the reasoning budget. Drives max-tokens per phase.
    effort: Literal["quick", "standard", "deep"] = "standard"


class ThinkResponse(BaseModel):
    success: bool
    session_id: str
    message: str


# ---------------------------------------------------------------------------
# Session snapshot — what /status and the initial SSE `snapshot` event return
# ---------------------------------------------------------------------------


class ThinkingPhaseSnapshot(BaseModel):
    name: str
    label: str
    status: Literal["pending", "in_progress", "completed", "failed", "skipped"]
    detail: Dict[str, Any] = Field(default_factory=dict)
    started_at: Optional[str] = None
    completed_at: Optional[str] = None


class ThinkingSessionSnapshot(BaseModel):
    session_id: str
    status: Literal["started", "in_progress", "completed", "failed"]
    progress: int
    prompt: str
    deliverable: DeliverableKind
    effort: Literal["quick", "standard", "deep"]
    provider: Literal["local", "claude"]
    current_phase: Optional[str] = None
    current_task: Optional[str] = None
    phases: List[ThinkingPhaseSnapshot] = Field(default_factory=list)
    clarifications: Dict[str, str] = Field(default_factory=dict)

    # Filled in progressively as phases complete
    understanding: Optional[str] = None
    assumptions: List[str] = Field(default_factory=list)
    constraints: List[str] = Field(default_factory=list)
    sub_questions: List[str] = Field(default_factory=list)
    alternatives: List[Dict[str, Any]] = Field(default_factory=list)
    decision: Optional[Dict[str, Any]] = None
    deliverable_markdown: Optional[str] = None
    critique: Optional[Dict[str, Any]] = None

    started_at: Optional[str] = None
    completed_at: Optional[str] = None
    error: Optional[str] = None
