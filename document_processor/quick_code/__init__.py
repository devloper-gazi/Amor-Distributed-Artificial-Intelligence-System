"""
QuickCode Mode — 5-phase reasoning-first lite pipeline.

Smarter than raw chat (it reasons + tests before delivering), faster
than the 9-phase Code Intelligence engine. Pluggable into Consortium
as a swap-in implementation engine.

Phases::

    Triage  →  Reason  →  Implement  →  Verify  →  Refine?

V2 — adapter layer adds (when ``settings.quick_v2_enabled=True``)::

    classify  →  striatum  →  ...  →  parsel  →  ...  →  sk_retrieve
       →  ...  →  symcode  →  verify  →  mcts  →  seeker  →  ...
       →  orpo  →  striatum_store

100% local — no paid APIs. No content filters.
"""

from .engine import QuickCodeEngine
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

# V2 module surface — re-exported for callers (Consortium, CLI, tests).
# Imports are lazy via module attributes so a partial install (no
# pydantic, no sympy) still lets the legacy dataclass surface work.
from . import (  # noqa: F401
    anton_brain,
    contracts,
    mcts,
    parsel,
    preferences,
    router,
    sandbox_tier,
    seeker,
    sk_coder,
    striatum,
    symcode,
)

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
    # V2 modules
    "anton_brain",
    "contracts",
    "mcts",
    "parsel",
    "preferences",
    "router",
    "sandbox_tier",
    "seeker",
    "sk_coder",
    "striatum",
    "symcode",
]
