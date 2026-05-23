"""
Cycle F Sprint 5 — approval-flow gate for MCP tool dispatch.

Wraps `ToolRegistry.dispatch()` with `ApprovalPolicy.decide()` so
every tool call passes through one of three buckets:

  * ``allow_silent`` — execute immediately (read-only file ops,
    metric reads, status queries).
  * ``deny`` — refuse without prompting (destructive ops the
    operator has pre-disallowed).
  * ``prompt`` — defer to the human (anything in between) via the
    SSE bridge.

Plus a cost circuit-breaker: per-session token budget that trips
when the agent accumulates too much spend.

OFF by default (`settings.code_approval_enabled=False`).  When off,
every tool runs as in Sprints 1-4 (no gating).

Public surface:

    ApprovalPolicy            # policy engine
    ApprovalDecision          # result of `decide()`
    ApprovalCategory          # category enum
    cost_circuit_breaker()    # helper for the per-session budget
    DEFAULT_POLICY            # process-wide singleton
    settings_to_policy(...)   # build from settings module
"""

from .policy import (
    ApprovalCategory,
    ApprovalDecision,
    ApprovalPolicy,
    ApprovalRequest,
    CostCircuitBreaker,
    DEFAULT_POLICY,
    cost_circuit_breaker,
    refresh_default_policy,
    settings_to_policy,
)

__all__ = [
    "ApprovalCategory",
    "ApprovalDecision",
    "ApprovalPolicy",
    "ApprovalRequest",
    "CostCircuitBreaker",
    "cost_circuit_breaker",
    "DEFAULT_POLICY",
    "refresh_default_policy",
    "settings_to_policy",
]
