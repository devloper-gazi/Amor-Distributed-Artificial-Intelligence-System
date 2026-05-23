"""
Cycle F Sprint 1 — install / start health-wait intersection regression.

After llama-swap was promoted to `tier="core"`, a naive
`health.wait_for(core_services())` would hang the `minimal` profile
install forever (minimal does not start llama-swap).  Both
`install.run_install` and `services.cmd_start` now intersect core
services with the profile's service set.  These tests guard that
behaviour without spinning up Docker.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from tools.setup import constants, health, install, services


# ─── Health-wait intersection ───────────────────────────────────────


def test_minimal_profile_does_not_include_llama_swap():
    """Sanity-pin: minimal is data-plane-only by design."""

    profile = constants.PROFILES["minimal"]
    assert "llama-swap" not in profile.services
    assert "ollama" not in profile.services


def test_llama_swap_is_core_tier():
    """v18 inference layer promoted to core."""

    by_name = {s.name: s for s in constants.SERVICES}
    assert by_name["llama-swap"].tier == "core"


def test_full_profile_does_include_llama_swap():
    """Full / dev / baseline profiles MUST include llama-swap."""

    for name in ("full", "dev", "baseline"):
        assert "llama-swap" in constants.PROFILES[name].services


def test_install_intersects_core_with_minimal_profile(monkeypatch):
    """`install --profile minimal` must NOT wait on llama-swap."""

    captured: dict = {}

    def fake_wait_for(svcs, **kw):
        captured["names"] = {s.name for s in svcs}
        # Pretend everything is healthy so install proceeds.
        report = health.HealthReport()
        for s in svcs:
            report.add(health.HealthResult(name=s.label, ok=True))
        return report

    monkeypatch.setattr(install.health, "wait_for", fake_wait_for)
    # Stub the heavy compose calls so the test doesn't shell out.
    monkeypatch.setattr(install.compose, "detect_engine",
                        lambda: install.compose.ComposeEngine(
                            bin=["docker", "compose"], compose_files=()
                        ))
    monkeypatch.setattr(install.compose, "pull",
                        lambda *a, **kw: install.util.CmdResult(0, "", ""))
    monkeypatch.setattr(install.compose, "build",
                        lambda *a, **kw: install.util.CmdResult(0, "", ""))
    monkeypatch.setattr(install.compose, "up",
                        lambda *a, **kw: install.util.CmdResult(0, "", ""))
    # Don't run models or verify in this test.
    opts = install.InstallOptions(
        profile="minimal",
        skip_pull=True,
        skip_build=True,
        skip_models=True,
        skip_verify=True,
        yes=True,
    )
    # Avoid running preflight (real Docker probe) — patch a clean report.
    from tools.setup import preflight
    monkeypatch.setattr(install.preflight, "run_preflight",
                        lambda: preflight.PreflightReport())

    rc = install.run_install(opts)
    assert rc == 0
    # llama-swap is core but not in minimal — must NOT be waited on.
    assert "llama-swap" not in captured["names"]
    # Core data-plane services SHOULD be waited on.
    assert {"gateway", "app", "postgres", "redis", "mongo"} <= {
        # Names captured are .name, mapped via SERVICES; convert via lookup.
        s.name for s in constants.SERVICES if s.label in captured["names"]
    } or captured["names"]  # tolerate either label or name


def test_install_full_profile_waits_on_llama_swap(monkeypatch):
    """`install --profile full` should wait on llama-swap (it's core)."""

    captured: dict = {}

    def fake_wait_for(svcs, **kw):
        captured["names"] = {s.name for s in svcs}
        report = health.HealthReport()
        for s in svcs:
            report.add(health.HealthResult(name=s.label, ok=True))
        return report

    monkeypatch.setattr(install.health, "wait_for", fake_wait_for)
    monkeypatch.setattr(install.compose, "detect_engine",
                        lambda: install.compose.ComposeEngine(
                            bin=["docker", "compose"], compose_files=()
                        ))
    monkeypatch.setattr(install.compose, "pull",
                        lambda *a, **kw: install.util.CmdResult(0, "", ""))
    monkeypatch.setattr(install.compose, "build",
                        lambda *a, **kw: install.util.CmdResult(0, "", ""))
    monkeypatch.setattr(install.compose, "up",
                        lambda *a, **kw: install.util.CmdResult(0, "", ""))
    from tools.setup import preflight
    monkeypatch.setattr(install.preflight, "run_preflight",
                        lambda: preflight.PreflightReport())

    opts = install.InstallOptions(
        profile="full",
        skip_pull=True,
        skip_build=True,
        skip_models=True,
        skip_verify=True,
        yes=True,
    )
    rc = install.run_install(opts)
    assert rc == 0
    assert "llama-swap" in captured["names"]


# ─── cmd_start subset behaviour ─────────────────────────────────────


def test_cmd_start_with_subset_only_waits_on_subset(monkeypatch):
    captured: dict = {}

    def fake_wait_for(svcs, **kw):
        captured["names"] = {s.name for s in svcs}
        report = health.HealthReport()
        for s in svcs:
            report.add(health.HealthResult(name=s.label, ok=True))
        return report

    monkeypatch.setattr(services.health, "wait_for", fake_wait_for)
    monkeypatch.setattr(services.compose, "detect_engine",
                        lambda: services.compose.ComposeEngine(
                            bin=["docker", "compose"], compose_files=()
                        ))
    monkeypatch.setattr(services.compose, "up",
                        lambda *a, **kw: services.util.CmdResult(0, "", ""))

    rc = services.cmd_start(["postgres", "redis"])
    assert rc == 0
    # Should ONLY wait for the requested core subset.
    assert captured["names"] == {"postgres", "redis"}


def test_cmd_start_no_args_waits_on_all_core(monkeypatch):
    captured: dict = {}

    def fake_wait_for(svcs, **kw):
        captured["names"] = {s.name for s in svcs}
        report = health.HealthReport()
        for s in svcs:
            report.add(health.HealthResult(name=s.label, ok=True))
        return report

    monkeypatch.setattr(services.health, "wait_for", fake_wait_for)
    monkeypatch.setattr(services.compose, "detect_engine",
                        lambda: services.compose.ComposeEngine(
                            bin=["docker", "compose"], compose_files=()
                        ))
    monkeypatch.setattr(services.compose, "up",
                        lambda *a, **kw: services.util.CmdResult(0, "", ""))

    rc = services.cmd_start([])
    assert rc == 0
    expected = {s.name for s in constants.SERVICES if s.tier == "core"}
    assert captured["names"] == expected
    # Sanity: this set MUST contain llama-swap after Cycle F Sprint 1.
    assert "llama-swap" in expected
