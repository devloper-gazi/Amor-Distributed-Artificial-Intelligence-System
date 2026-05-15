"""
Cycle F Sprint 5 — ApprovalPolicy class.

Three-list policy + cost circuit-breaker, modelled after Roo /
OpenHands ConfirmRisky patterns.  Pure Python — no I/O, no HTTP,
no SSE plumbing.  The runtime path that bridges policy decisions
to user prompts lives in `local_ai/approval/router.py`
(Sprint 5 Day 2 — landed alongside this file).

The categorisation of a tool call follows this order:

  1. **Exact tool-name match** in `allow_silent` / `deny`.
  2. **Category lookup** via the tool's declared
     ``tool.category`` attribute (Sprint 5 convention — defaults to
     ``"unclassified"``).  Category ∈ allow_silent / deny / prompt.
  3. **Default behaviour** — `default_action` (configurable;
     "prompt" is the safe default).

Per-session token budget tripping is an orthogonal concern handled
by `cost_circuit_breaker()` — a helper that subtracts per-call
costs and returns True when the budget is exhausted.  The caller
combines both signals: if EITHER policy denies OR the budget is
exhausted, the call is blocked.
"""

from __future__ import annotations

import enum
import logging
from dataclasses import dataclass, field
from typing import Any, Mapping

logger = logging.getLogger(__name__)


class ApprovalCategory(str, enum.Enum):
    """Built-in categories every tool can declare.  Operators
    customise the three-list mapping via settings; categories are
    just convenient labels."""

    READ = "read"              # read_file, search_codebase, list_dir
    WRITE = "write"            # file.write, edit_file
    DELETE = "delete"          # file.delete, rm -rf
    EXEC = "exec"              # shell.exec, sandbox.execute
    NETWORK = "network"        # http_get, scrape
    GIT = "git"                # git.push, git.reset
    DB = "db"                  # DROP TABLE, ALTER TABLE
    DOCKER = "docker"          # docker.run / exec
    LLM = "llm"                # nested LLM calls
    SECRET = "secret"          # read_secret, set_secret
    PACKAGE = "package"        # pip.install, npm.install
    UNCLASSIFIED = "unclassified"


# Default category → ApprovalAction mapping for the three lists.
# Operators override via settings; this is the conservative default.
_DEFAULT_CATEGORY_ACTIONS: dict[ApprovalCategory, str] = {
    ApprovalCategory.READ: "allow_silent",
    ApprovalCategory.WRITE: "prompt",
    ApprovalCategory.DELETE: "deny",          # destructive — denied by default
    ApprovalCategory.EXEC: "prompt",
    ApprovalCategory.NETWORK: "prompt",
    ApprovalCategory.GIT: "prompt",
    ApprovalCategory.DB: "deny",              # destructive
    ApprovalCategory.DOCKER: "prompt",
    ApprovalCategory.LLM: "allow_silent",
    ApprovalCategory.SECRET: "deny",          # never auto-approve
    ApprovalCategory.PACKAGE: "prompt",
    ApprovalCategory.UNCLASSIFIED: "prompt",
}


class ApprovalDecision(str, enum.Enum):
    """Outcome of `ApprovalPolicy.decide()`."""

    ALLOW = "allow"           # execute without prompting
    PROMPT = "prompt"         # defer to the human via SSE bridge
    DENY = "deny"             # refuse — return ToolError-equivalent
    BUDGET_EXCEEDED = "budget_exceeded"  # cost circuit-breaker tripped


@dataclass
class ApprovalRequest:
    """Inputs to `decide()` — one per tool dispatch."""

    tool_name: str
    category: str = ApprovalCategory.UNCLASSIFIED.value
    arguments: Mapping[str, Any] = field(default_factory=dict)
    session_id: str | None = None
    actor_role: str | None = None       # active LLM role at call site
    estimated_cost_tokens: int = 0      # for the circuit-breaker


@dataclass
class ApprovalPolicy:
    """Three-list approval policy + helpers.

    Construction:
      ApprovalPolicy(
          allow_silent_tools={"read_file", "search_codebase"},
          deny_tools={"shell.rm_rf"},
          prompt_tools={...},  # optional; everything not in
                               # allow_silent + deny falls here
                               # via category → action mapping
          category_actions={...},
          default_action="prompt",
          enabled=True,
      )

    Decisions:
      decide(ApprovalRequest) -> ApprovalDecision
    """

    allow_silent_tools: set[str] = field(default_factory=set)
    deny_tools: set[str] = field(default_factory=set)
    prompt_tools: set[str] = field(default_factory=set)
    category_actions: dict[str, str] = field(
        default_factory=lambda: {
            cat.value: act for cat, act in _DEFAULT_CATEGORY_ACTIONS.items()
        }
    )
    default_action: str = "prompt"   # allow / prompt / deny
    enabled: bool = False

    def decide(self, req: ApprovalRequest) -> ApprovalDecision:
        """Map a request to one of `ApprovalDecision`.

        Resolution order:
          1. If policy is disabled → ALLOW (zero-cost passthrough).
          2. Tool-name exact-match in `deny_tools` → DENY.
          3. Tool-name exact-match in `allow_silent_tools` → ALLOW.
          4. Tool-name exact-match in `prompt_tools` → PROMPT.
          5. Category action lookup.
          6. Default action.
        """

        if not self.enabled:
            return ApprovalDecision.ALLOW

        if req.tool_name in self.deny_tools:
            return ApprovalDecision.DENY
        if req.tool_name in self.allow_silent_tools:
            return ApprovalDecision.ALLOW
        if req.tool_name in self.prompt_tools:
            return ApprovalDecision.PROMPT

        # Category lookup.
        cat = (req.category or ApprovalCategory.UNCLASSIFIED.value).lower()
        action = self.category_actions.get(cat)
        if action is None:
            action = self.default_action

        return self._action_to_decision(action)

    @staticmethod
    def _action_to_decision(action: str) -> ApprovalDecision:
        action = (action or "").strip().lower()
        if action == "allow_silent" or action == "allow":
            return ApprovalDecision.ALLOW
        if action == "deny":
            return ApprovalDecision.DENY
        return ApprovalDecision.PROMPT


# ─── Cost circuit-breaker ───────────────────────────────────────────


@dataclass
class CostCircuitBreaker:
    """Per-session token budget.  Trip once exceeded; can be reset
    explicitly when a new session begins."""

    budget_tokens: int = 50_000
    spent_tokens: int = 0
    tripped: bool = False

    def charge(self, tokens: int) -> bool:
        """Charge `tokens` against the budget.  Returns True when
        the call should be ALLOWED (budget intact); False when
        tripped.  Idempotent once tripped."""

        if self.tripped:
            return False
        self.spent_tokens += max(0, int(tokens))
        if self.spent_tokens >= self.budget_tokens:
            self.tripped = True
            logger.warning(
                "cost_circuit_breaker_tripped budget=%d spent=%d",
                self.budget_tokens, self.spent_tokens,
            )
            return False
        return True

    def reset(self, budget_tokens: int | None = None) -> None:
        self.spent_tokens = 0
        self.tripped = False
        if budget_tokens is not None:
            self.budget_tokens = int(budget_tokens)


def cost_circuit_breaker(
    *, budget_tokens: int = 50_000,
) -> CostCircuitBreaker:
    """Construct a per-session cost-circuit-breaker."""

    return CostCircuitBreaker(budget_tokens=budget_tokens)


# ─── Settings -> Policy ─────────────────────────────────────────────


def settings_to_policy(
    *,
    enabled: bool,
    allow_silent_csv: str = "",
    deny_csv: str = "",
    prompt_csv: str = "",
    default_action: str = "prompt",
    category_actions_json: str | dict | None = None,
) -> ApprovalPolicy:
    """Build an `ApprovalPolicy` from the runtime settings shape.

    Tolerant of empty / malformed inputs — defaults take over.
    """

    import json as _json

    def _csv_to_set(s: str) -> set[str]:
        if not s:
            return set()
        return {item.strip() for item in s.split(",") if item.strip()}

    allow_silent = _csv_to_set(allow_silent_csv)
    deny = _csv_to_set(deny_csv)
    prompt = _csv_to_set(prompt_csv)

    # Category-action map: start with defaults, override from settings.
    cat_actions: dict[str, str] = {
        cat.value: act for cat, act in _DEFAULT_CATEGORY_ACTIONS.items()
    }
    if category_actions_json:
        try:
            raw = (
                category_actions_json
                if isinstance(category_actions_json, dict)
                else _json.loads(category_actions_json)
            )
            if isinstance(raw, dict):
                for k, v in raw.items():
                    if isinstance(k, str) and isinstance(v, str):
                        cat_actions[k.strip().lower()] = v.strip().lower()
        except (ValueError, _json.JSONDecodeError) as exc:
            logger.warning(
                "approval: category_actions JSON parse failed: %s", exc,
            )

    return ApprovalPolicy(
        allow_silent_tools=allow_silent,
        deny_tools=deny,
        prompt_tools=prompt,
        category_actions=cat_actions,
        default_action=default_action,
        enabled=bool(enabled),
    )


# Module-level singleton.  Initialised in the disabled state so
# Sprints 1-4 deploys behave unchanged.  `refresh_default_policy()`
# rebuilds it from `document_processor.config.settings` lazily.
DEFAULT_POLICY = ApprovalPolicy()  # disabled by default

# Cached snapshot of the settings inputs used to build DEFAULT_POLICY
# (so we can skip the rebuild when nothing changed).
_CACHED_SETTINGS_KEY: tuple = ()


def refresh_default_policy(settings_obj: Any = None) -> ApprovalPolicy:
    """Read `settings.code_approval_*` and rebuild `DEFAULT_POLICY`
    in place if any of the inputs changed.  Cheap: a settings-hash
    check + occasional ApprovalPolicy() construction.

    Caller can pass an explicit settings_obj (useful for tests);
    when None, attempts to import the AMOR settings module.

    Returns the resulting policy (same object as DEFAULT_POLICY).
    """

    global DEFAULT_POLICY, _CACHED_SETTINGS_KEY
    if settings_obj is None:
        try:
            from document_processor.config.settings import (  # noqa: PLC0415
                settings as _amor_settings,
            )
            settings_obj = _amor_settings
        except Exception:
            return DEFAULT_POLICY

    key = (
        bool(getattr(settings_obj, "code_approval_enabled", False)),
        str(getattr(settings_obj, "code_approval_allow_silent", "")),
        str(getattr(settings_obj, "code_approval_deny", "")),
        str(getattr(settings_obj, "code_approval_prompt", "")),
        str(getattr(settings_obj, "code_approval_default_action", "prompt")),
        str(getattr(settings_obj, "code_approval_category_actions", "")),
    )
    if key == _CACHED_SETTINGS_KEY and DEFAULT_POLICY.enabled is key[0]:
        return DEFAULT_POLICY

    new_policy = settings_to_policy(
        enabled=key[0],
        allow_silent_csv=key[1],
        deny_csv=key[2],
        prompt_csv=key[3],
        default_action=key[4],
        category_actions_json=key[5] or None,
    )
    # Mutate the shared singleton in-place so existing references
    # (`from local_ai.approval import DEFAULT_POLICY`) pick up the
    # new state without re-importing.
    DEFAULT_POLICY.allow_silent_tools = new_policy.allow_silent_tools
    DEFAULT_POLICY.deny_tools = new_policy.deny_tools
    DEFAULT_POLICY.prompt_tools = new_policy.prompt_tools
    DEFAULT_POLICY.category_actions = new_policy.category_actions
    DEFAULT_POLICY.default_action = new_policy.default_action
    DEFAULT_POLICY.enabled = new_policy.enabled
    _CACHED_SETTINGS_KEY = key
    logger.info(
        "approval_policy_refreshed enabled=%s allow=%d deny=%d prompt=%d",
        new_policy.enabled, len(new_policy.allow_silent_tools),
        len(new_policy.deny_tools), len(new_policy.prompt_tools),
    )
    return DEFAULT_POLICY


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
