"""Tests for plugin registries (Charter §6 Mandate 1)."""

from __future__ import annotations

from typing import Any

import pytest

from document_processor.code_intelligence.registries import (
    AgentRegistry,
    CapabilitySourceRegistry,
    Registry,
    SandboxTier,
    SandboxTierRegistry,
    register_default_sources,
    register_default_tiers,
    register_defaults,
)


# ── Generic Registry primitive ─────────────────────────────────────────


def test_registry_register_and_get() -> None:
    reg: Registry[str] = Registry("widget")
    reg.register("a", "hello")
    assert reg.get("a") == "hello"
    assert reg.require("a") == "hello"
    assert "a" in reg
    assert len(reg) == 1


def test_registry_get_missing_returns_none() -> None:
    reg: Registry[str] = Registry("widget")
    assert reg.get("missing") is None
    with pytest.raises(KeyError):
        reg.require("missing")


def test_registry_duplicate_register_raises_unless_replace() -> None:
    reg: Registry[str] = Registry("widget")
    reg.register("a", "first")
    with pytest.raises(ValueError, match="already registered"):
        reg.register("a", "second")
    # replace=True allows the override
    reg.register("a", "second", replace=True)
    assert reg.get("a") == "second"


def test_registry_unregister() -> None:
    reg: Registry[str] = Registry("widget")
    reg.register("a", "x")
    assert reg.unregister("a") is True
    assert reg.unregister("a") is False
    assert "a" not in reg


def test_registry_names_sorted() -> None:
    reg: Registry[int] = Registry("n")
    reg.register("c", 3)
    reg.register("a", 1)
    reg.register("b", 2)
    assert reg.names() == ["a", "b", "c"]


def test_registry_empty_name_rejected() -> None:
    reg: Registry[int] = Registry("n")
    with pytest.raises(ValueError):
        reg.register("", 1)


# ── AgentRegistry ──────────────────────────────────────────────────────


def test_agent_registry_register_defaults_idempotent() -> None:
    # Use a fresh registry to avoid bleed from other tests.
    reg = AgentRegistry()
    assert len(reg) == 0
    # The module-level register_defaults populates the *singleton*; here
    # we verify the role-name path on a fresh registry.
    reg.register_role("planner", lambda **_: "p")
    reg.register_role("coder", lambda **_: "c")
    assert "planner" in reg
    assert "coder" in reg
    assert len(reg) == 2


def test_register_defaults_populates_singleton() -> None:
    """The module-level singleton picks up all five canonical roles
    after register_defaults() runs (idempotent)."""
    register_defaults()
    register_defaults()  # second call must not raise
    from document_processor.code_intelligence.registries import agent_registry

    for role in ("planner", "coder", "tester", "debugger", "critic"):
        assert role in agent_registry, f"missing role: {role}"


# ── SandboxTierRegistry ────────────────────────────────────────────────


def test_sandbox_tier_register_and_lookup() -> None:
    reg = SandboxTierRegistry()
    tier = SandboxTier(
        tier=42,
        name="custom",
        description="test tier",
        isolation="container",
    )
    reg.register_tier(tier)
    assert reg.get("custom") is tier
    assert reg.by_tier_number(42) is tier


def test_sandbox_tier_register_defaults_has_three_tiers() -> None:
    register_default_tiers()
    register_default_tiers()  # idempotent
    from document_processor.code_intelligence.registries import sandbox_tier_registry

    for name in ("docker", "firecracker", "gvisor"):
        assert name in sandbox_tier_registry, f"missing tier: {name}"
    docker = sandbox_tier_registry.require("docker")
    assert docker.available is True
    assert docker.network == "none"
    fc = sandbox_tier_registry.require("firecracker")
    assert fc.available is False  # placeholder until v2.1
    assert fc.factory is None


def test_sandbox_tier_lookup_by_number_returns_none_for_unknown() -> None:
    reg = SandboxTierRegistry()
    reg.register_tier(SandboxTier(tier=1, name="a", description="",
                                   isolation="container"))
    assert reg.by_tier_number(99) is None


# ── CapabilitySourceRegistry ───────────────────────────────────────────


@pytest.mark.asyncio
async def test_capability_source_register_and_invoke() -> None:
    reg = CapabilitySourceRegistry()

    async def fake_source(*_args: Any, **_kwargs: Any) -> list[Any]:
        return ["candidate-a", "candidate-b"]

    reg.register("fake", fake_source)
    src = reg.require("fake")
    result = await src()
    assert result == ["candidate-a", "candidate-b"]


def test_register_default_sources_has_three() -> None:
    register_default_sources()
    register_default_sources()  # idempotent
    from document_processor.code_intelligence.registries import (
        capability_source_registry,
    )

    for name in ("huggingface", "github", "arxiv"):
        assert name in capability_source_registry, f"missing: {name}"


def test_capability_source_unregister_then_re_register_with_new_factory() -> None:
    reg = CapabilitySourceRegistry()

    async def src_v1(*_a: Any, **_k: Any) -> list[Any]:
        return ["v1"]

    async def src_v2(*_a: Any, **_k: Any) -> list[Any]:
        return ["v2"]

    reg.register("test_source", src_v1)
    reg.unregister("test_source")
    reg.register("test_source", src_v2)
    assert reg.require("test_source") is src_v2
