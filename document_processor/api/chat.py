"""Cycle UI 2026-05-20 — Unified chat endpoints.

This module fronts the 6 mode-specific routes
(`/api/code`, `/api/research`, `/api/thinking`, `/api/consortium`,
`/api/sentinel`, `/api/quick-code`) with a single thin dispatcher
that the new unified SPA can talk to.

Phase 1 ships only the **classifier endpoint** — frontend posts a
prompt + receives the auto-detected mode + alternatives.  Phase 2
will add `POST /api/chat/start` (dispatches to one of the 6 mode
handlers) and `GET /api/chat/{sid}/events` (proxies to the mode's
existing SSE feed based on the persisted `chat_session.mode`).

Auth: reuses `get_current_user` so the same JWT cookie / Bearer
token that protects the rest of `/api/*` covers these endpoints.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict, List, Optional, Tuple

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from ..auth.dependencies import get_current_user
from ..auth.models import User
from ..services.intent_classifier import (
    CLASSES,
    IntentResult,
    get_classifier,
)


logger = logging.getLogger(__name__)


router = APIRouter(prefix="/api/chat", tags=["chat"])


# ─── Pydantic request / response shapes ────────────────────────────────


class ClassifyRequest(BaseModel):
    """Body of POST /api/chat/classify.

    The composer calls this on a 150-ms debounce while the user types;
    the response drives the auto-mode preview pill.
    """

    prompt: str = Field(
        ...,
        min_length=1,
        max_length=8000,
        description="Prompt to classify.  Empty / whitespace-only "
                    "prompts return the fallback class with "
                    "low_confidence=True.",
    )


class ClassifyResponse(BaseModel):
    mode: str = Field(..., description="The winning class (lowercase).")
    top1_score: float
    top2_score: float
    confidence: float = Field(
        ...,
        description="Heuristic [0, 1] from top1-top2 gap. Not a "
                    "calibrated probability.",
    )
    low_confidence: bool = Field(
        ...,
        description="True when the composer should show the "
                    "disambiguation pill instead of routing silently.",
    )
    alternatives: List[Tuple[str, float]] = Field(
        default_factory=list,
        description="All 6 (class, score) pairs sorted descending.",
    )
    latency_ms: float


# ─── Endpoint ──────────────────────────────────────────────────────────


def _serialize(result: IntentResult) -> Dict[str, Any]:
    """Translate IntentResult → API shape.  Alternative tuples are kept
    as 2-element lists in JSON for client friendliness (TypeScript /
    JS handles ``[string, number]`` cleanly, but not Python tuples)."""
    return {
        "mode": result.mode,
        "top1_score": round(result.top1_score, 4),
        "top2_score": round(result.top2_score, 4),
        "confidence": round(result.confidence, 4),
        "low_confidence": result.low_confidence,
        "alternatives": [
            [cls, round(score, 4)] for cls, score in result.alternatives
        ],
        "latency_ms": round(result.latency_ms, 2),
    }


@router.post("/classify")
async def classify_prompt(
    req: ClassifyRequest,
    _user: User = Depends(get_current_user),
) -> Dict[str, Any]:
    """Run the auto-mode classifier on a single prompt.

    The classifier is a process-wide singleton; the first call after
    boot triggers a 3-5 s MiniLM-L6 load, subsequent calls run in
    10-100 ms on CPU.  We always return a result — even pathological
    inputs (empty, whitespace-only) get the fallback class plus
    ``low_confidence=True`` so the composer's preview pill can render.
    """
    classifier = get_classifier()
    # The classifier itself is sync (encoder.encode runs CPU-bound
    # PyTorch); push the call into an executor so the FastAPI worker
    # event loop stays free under concurrent traffic.
    loop = asyncio.get_running_loop()
    try:
        result: IntentResult = await loop.run_in_executor(
            None, classifier.classify, req.prompt,
        )
    except Exception as exc:  # pragma: no cover - infra surface
        logger.exception("classify_failed prompt_len=%d", len(req.prompt))
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"intent classifier failed: {type(exc).__name__}",
        )
    return _serialize(result)


@router.get("/classes")
async def list_classes(
    _user: User = Depends(get_current_user),
) -> Dict[str, Any]:
    """Return the static list of classifier classes the composer can
    surface in its mode picker.  Used by frontend i18n / chip layout
    to avoid hardcoding the class set in two places."""
    return {"classes": list(CLASSES)}
