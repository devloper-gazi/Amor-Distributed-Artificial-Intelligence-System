"""
QuickCode Mode — 5-phase reasoning-first lite pipeline.

Smarter than raw chat (it reasons + tests before delivering), faster
than the 9-phase Code Intelligence engine. Pluggable into Consortium
as a swap-in implementation engine.

Phases::

    Triage  →  Reason  →  Implement  →  Verify  →  Refine?

100% local — no paid APIs.
"""

from .models import (
    COMPOSITE_WEIGHTS,
    MAX_REFINE_ITERATIONS,
    QuickCodeAlternative,
    QuickCodeBundle,
    QuickCodeGate,
    QuickCodeReasoning,
    QuickCodeRequest,
    QuickCodeVerification,
)
from .engine import QuickCodeEngine

__all__ = [
    "COMPOSITE_WEIGHTS",
    "MAX_REFINE_ITERATIONS",
    "QuickCodeAlternative",
    "QuickCodeBundle",
    "QuickCodeEngine",
    "QuickCodeGate",
    "QuickCodeReasoning",
    "QuickCodeRequest",
    "QuickCodeVerification",
]
