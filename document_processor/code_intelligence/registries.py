"""
Plugin registries — Charter §6 Mandate 1.

Three registries, one for each extensible subsystem:

  AgentRegistry          — Planner / Coder / Tester / Debugger / Critic
                           by role name. The engine consults this
                           registry instead of importing concrete agent
                           classes, so a future engineer can drop a
                           ``ReviewerAgent`` into
                           ``code_intelligence/extensions/`` and have
                           it picked up on next reload.
  SandboxTierRegistry    — Tier 1 (current Docker), Tier 2 (planned
                           Firecracker), Tier 3 (planned gVisor) — by
                           integer tier number.
  CapabilitySourceRegistry — Hugging Face / GitHub / arXiv / awesome-
                              lists — by source name. The
                              CapabilityDiscoverer iterates registered
                              sources rather than hard-coding three
                              imports.

The registries are deliberately simple: a name-keyed dict + a register
decorator + a get / list API. No automatic discovery (that would
introduce import-time side effects and a debugging nightmare). Plugins
register themselves at import time; the engine triggers imports of
the ``extensions/`` package once at startup.

Pattern matches the FastAPI router-registration model the rest of the
codebase uses.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Generic, TypeVar

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# Generic registry primitive
# ─────────────────────────────────────────────────────────────────────────────


T = TypeVar("T")


class Registry(Generic[T]):
    """Generic name-keyed plugin registry. All three concrete
    registries below extend this with a typed register/get API."""

    def __init__(self, label: str) -> None:
        self._label = label
        self._items: dict[str, T] = {}

    def register(self, name: str, item: T, *, replace: bool = False) -> T:
        """Register ``item`` under ``name``. Second registration with the
        same name raises ``ValueError`` unless ``replace=True`` — this
        is intentional: silent shadowing of plugins is a debugging trap.
        """
        if not name:
            raise ValueError("registry name cannot be empty")
        if name in self._items and not replace:
            raise ValueError(
                f"{self._label} '{name}' already registered; pass replace=True to override",
            )
        self._items[name] = item
        logger.info("%s_registered name=%s replace=%s", self._label, name, replace)
        return item

    def get(self, name: str) -> T | None:
        return self._items.get(name)

    def require(self, name: str) -> T:
        item = self._items.get(name)
        if item is None:
            raise KeyError(f"{self._label} '{name}' not registered")
        return item

    def names(self) -> list[str]:
        return sorted(self._items.keys())

    def items(self) -> dict[str, T]:
        return dict(self._items)

    def unregister(self, name: str) -> bool:
        if name in self._items:
            del self._items[name]
            logger.info("%s_unregistered name=%s", self._label, name)
            return True
        return False

    def __len__(self) -> int:
        return len(self._items)

    def __contains__(self, name: str) -> bool:
        return name in self._items


# ─────────────────────────────────────────────────────────────────────────────
# AgentRegistry
# ─────────────────────────────────────────────────────────────────────────────


# An agent factory takes the injected ``llm_call`` + ``max_tokens`` and
# returns an instance with a ``run(ctx)`` method. The engine doesn't care
# what concrete class it is — it just calls the factory and the result.
AgentFactory = Callable[..., Any]


class AgentRegistry(Registry[AgentFactory]):
    """Registry of agent factories keyed by role.

    Default registrations (planner / coder / tester / debugger / critic)
    are populated at module import time by ``register_defaults()``
    below. Tests can register mocked agents under different role names
    without touching the v1 implementations.
    """

    def __init__(self) -> None:
        super().__init__("agent")

    def register_role(
        self,
        role: str,
        factory: AgentFactory,
        *,
        replace: bool = False,
    ) -> AgentFactory:
        return self.register(role, factory, replace=replace)


# Module-level singleton.
agent_registry = AgentRegistry()


def register_defaults() -> None:
    """
    Register the v1 specialist agents under their canonical role names.
    Called by the engine at construction; idempotent.
    """
    if "planner" in agent_registry:
        return
    from .agents import (
        CoderAgent,
        CriticAgent,
        DebuggerAgent,
        PlannerAgent,
        TesterAgent,
    )

    agent_registry.register_role("planner", PlannerAgent)
    agent_registry.register_role("coder", CoderAgent)
    agent_registry.register_role("tester", TesterAgent)
    agent_registry.register_role("debugger", DebuggerAgent)
    agent_registry.register_role("critic", CriticAgent)


# ─────────────────────────────────────────────────────────────────────────────
# SandboxTierRegistry
# ─────────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class SandboxTier:
    """Static metadata for a sandbox tier."""

    tier: int  # 1, 2, 3 ...
    name: str  # "docker", "firecracker", "gvisor"
    description: str
    isolation: str  # "container" | "microvm" | "user-space"
    network: str = "none"
    factory: Callable[..., Any] | None = None
    available: bool = True


class SandboxTierRegistry(Registry[SandboxTier]):
    """Registry of sandbox tiers. Tier 1 (Docker) is registered by
    default; Tiers 2+ are placeholders for v2.1 extensions."""

    def __init__(self) -> None:
        super().__init__("sandbox_tier")

    def register_tier(
        self,
        tier: SandboxTier,
        *,
        replace: bool = False,
    ) -> SandboxTier:
        return self.register(tier.name, tier, replace=replace)

    def by_tier_number(self, tier: int) -> SandboxTier | None:
        for t in self._items.values():
            if t.tier == tier:
                return t
        return None


sandbox_tier_registry = SandboxTierRegistry()


def register_default_tiers() -> None:
    """Register the v1 Docker tier + placeholders for Firecracker / gVisor."""
    if "docker" in sandbox_tier_registry:
        return

    # The Tier-1 factory is the existing ExecutionSandbox class — a
    # plain callable that takes the standard kwargs and returns a
    # configured instance.
    from .sandbox import ExecutionSandbox

    sandbox_tier_registry.register_tier(
        SandboxTier(
            tier=1,
            name="docker",
            description="Docker container with --network none + read-only mount",
            isolation="container",
            network="none",
            factory=ExecutionSandbox,
            available=True,
        ),
    )
    # Placeholders — visible via /sandbox/health diagnostics, not active.
    sandbox_tier_registry.register_tier(
        SandboxTier(
            tier=2,
            name="firecracker",
            description="microVM via Firecracker (planned v2.1)",
            isolation="microvm",
            network="none",
            factory=None,
            available=False,
        ),
    )
    sandbox_tier_registry.register_tier(
        SandboxTier(
            tier=3,
            name="gvisor",
            description="user-space kernel (gVisor) (planned v2.1)",
            isolation="user-space",
            network="none",
            factory=None,
            available=False,
        ),
    )


# ─────────────────────────────────────────────────────────────────────────────
# CapabilitySourceRegistry
# ─────────────────────────────────────────────────────────────────────────────


# A discovery source is an async callable returning a list of
# CapabilityCandidate. Signature: (...) -> Awaitable[list].
CapabilitySource = Callable[..., Awaitable[list[Any]]]


class CapabilitySourceRegistry(Registry[CapabilitySource]):
    """Registry of discovery sources. The CapabilityDiscoverer iterates
    registered sources each cycle. A future engineer adds a new source
    (say, GitLab) by writing `_discover_gitlab` and registering it
    under "gitlab"; no change to the discoverer required."""

    def __init__(self) -> None:
        super().__init__("capability_source")


capability_source_registry = CapabilitySourceRegistry()


def register_default_sources() -> None:
    """Register the v1 sources (HF / GitHub / arXiv)."""
    if "huggingface" in capability_source_registry:
        return
    from .capability_discoverer import (
        _discover_arxiv,
        _discover_github,
        _discover_hugging_face,
    )

    capability_source_registry.register("huggingface", _discover_hugging_face)
    capability_source_registry.register("github", _discover_github)
    capability_source_registry.register("arxiv", _discover_arxiv)
