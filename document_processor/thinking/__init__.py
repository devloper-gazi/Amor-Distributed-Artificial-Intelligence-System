"""
Thinking Mode — human-in-the-loop deep reasoning pipeline.

Unlike research (which gathers external knowledge) or simple chat, Thinking
Mode is designed for *complex, specific requests* (architecture design,
algorithm selection, system trade-offs, code planning). It asks clarifying
questions *before* it commits to an answer, then walks through a structured
multi-phase reasoning process:

    Understand → Decompose → Explore alternatives → Evaluate → Synthesize → Review

The whole pipeline streams over SSE and is scoped per user.
"""

from .engine import ThinkingEngine, ThinkingPhase
from .models import (
    AnalyzeRequest,
    AnalyzeResponse,
    ClarifyingQuestion,
    ThinkRequest,
    ThinkResponse,
    ThinkingSessionSnapshot,
)

__all__ = [
    "ThinkingEngine",
    "ThinkingPhase",
    "AnalyzeRequest",
    "AnalyzeResponse",
    "ClarifyingQuestion",
    "ThinkRequest",
    "ThinkResponse",
    "ThinkingSessionSnapshot",
]
