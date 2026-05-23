"""
Cycle F Sprint 5 — SSE-based approval bridge.

Wires the `ApprovalPolicy.PROMPT` decisions from
`local_ai.tools.registry.dispatch` to the browser via the existing
code-intelligence SSE channel.  Flow:

  1. Engine attempts a tool dispatch.
  2. Policy returns PROMPT → registry awaits `approval_callback`.
  3. The bridge publishes an `approval_required` SSE event on the
     session's channel + stores the open request in Redis with a
     short TTL.
  4. The browser shows an inline approval card and POSTs to
     `/api/approval/{request_id}` with `{approved: bool}`.
  5. The route resolves the awaiting future; registry continues.

Public surface:
    AwaitingApproval                     # dataclass record
    request_user_approval(...)           # await this from tools
    resolve_approval(request_id, ok)     # called by the HTTP route
    register_approval_routes(app)        # FastAPI router include
"""

from .bridge import (
    AwaitingApproval,
    pending_count,
    request_user_approval,
    resolve_approval,
)
from .routes import register_approval_routes, router as approval_router

__all__ = [
    "AwaitingApproval",
    "approval_router",
    "pending_count",
    "register_approval_routes",
    "request_user_approval",
    "resolve_approval",
]
