"""
Sentinel Evolution Engine — nine subsystems that let Sentinel
self-improve over time.

Pillars (per Phase 15 spec):

* Subsystem I — **Governance & immutable ledger** (this commit)
* Subsystem A — Preference logging (this commit)
* Subsystem C — Prompt evolution (DSPy-lite + genetic + adversarial)
* Subsystem D — Detection-rule synthesis (Semgrep YAML)
* Subsystem E — Dynamic agent spawning (MetaMonitor + AgentFactory)
* Subsystem G — Curriculum-driven self-play (4 difficulty levels)
* Subsystem B — QLoRA fine-tuning (orchestrator; training optional)
* Subsystem F — Knowledge distillation (FastJudge / MicroAuditor)
* Subsystem H — DAG architecture mutation (replay-tested mutants)

The governance layer is the safety floor: every other subsystem
records its actions to the immutable ledger, evaluates inside the
sandbox, honours the hard-coded constraints, and can be rolled
back to any prior state.

License: MIT.
"""

from __future__ import annotations

from .governance import (
    GovernanceError,
    HardConstraintViolation,
    ImmutableConstraints,
    LedgerEntry,
    LedgerStore,
    SandboxViolation,
    load_immutable_constraints,
)
from .preferences import (
    PreferencePair,
    PreferenceStore,
    UserAction,
)


__all__ = [
    "GovernanceError",
    "HardConstraintViolation",
    "ImmutableConstraints",
    "LedgerEntry",
    "LedgerStore",
    "PreferencePair",
    "PreferenceStore",
    "SandboxViolation",
    "UserAction",
    "load_immutable_constraints",
]
