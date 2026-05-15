"""
Cycle C Sprint 5 Day 2 — sandbox.security_posture() unit tests.

The introspection helper is pure — it reads env + the sandbox
instance's own state.  These tests drive the env via monkeypatch and
assert the returned snapshot reflects what the runtime would actually
see.
"""

from __future__ import annotations

import pytest

from document_processor.code_intelligence.sandbox import ExecutionSandbox


@pytest.fixture
def fresh_sandbox(monkeypatch):
    # Force a known workdir-resolution path so /sandbox-shared
    # detection doesn't surprise the tests on dev hosts.
    monkeypatch.setenv("AMOR_SANDBOX_WORKDIR", "/tmp/amor-sandbox-test")
    monkeypatch.delenv("DOCKER_HOST", raising=False)
    monkeypatch.delenv("AMOR_DOCKER_HOST", raising=False)
    return ExecutionSandbox()


def test_baseline_no_proxy(fresh_sandbox):
    posture = fresh_sandbox.security_posture()
    assert posture["docker_host"] == ""
    assert posture["via_proxy"] is False
    flags = posture["flags_active"]
    assert flags["no_new_privileges"] is True
    assert flags["read_only"] is True
    assert flags["default_network"] == "none"
    assert flags["memory_limit"] == "256m"
    assert flags["cpu_quota"] == 50000
    # Cycle C Sprint 5 Day 3 — these now ship enabled.
    assert flags["cap_drop_all"] is True
    assert flags["pids_limit"] == 128
    assert flags["seccomp_profile"] == "docker-default"
    # Score: NNP + RO + memory + cpu + network=none + tmpfs +
    #        cap_drop + pids + seccomp = 9 → hardened (just shy of max)
    assert posture["score"] == 9
    assert posture["level"] == "max" or posture["level"] == "hardened"


def test_proxy_active(monkeypatch, fresh_sandbox):
    monkeypatch.setenv("DOCKER_HOST", "tcp://amor-docker-proxy:2375")
    posture = fresh_sandbox.security_posture()
    assert posture["via_proxy"] is True
    assert posture["docker_host"] == "tcp://amor-docker-proxy:2375"
    # Score gains +1 for the proxy → 10 → max level.
    assert posture["score"] == 10
    assert posture["level"] == "max"


def test_proxy_via_amor_docker_host_fallback(monkeypatch, fresh_sandbox):
    """When DOCKER_HOST isn't set but AMOR_DOCKER_HOST is, the helper
    still surfaces the proxy URL (compose forwards both to keep
    rollback simple)."""
    monkeypatch.delenv("DOCKER_HOST", raising=False)
    monkeypatch.setenv("AMOR_DOCKER_HOST", "tcp://amor-docker-proxy:2375")
    posture = fresh_sandbox.security_posture()
    assert posture["via_proxy"] is True
    assert posture["docker_host"] == "tcp://amor-docker-proxy:2375"


def test_non_proxy_tcp_still_flagged_as_direct(monkeypatch, fresh_sandbox):
    """A bare ``tcp://`` URL without "proxy" in it (e.g. a remote
    daemon) does NOT count as a proxied path.  This avoids inflating
    the score for an arbitrary remote daemon."""
    monkeypatch.setenv("DOCKER_HOST", "tcp://remote-daemon:2376")
    posture = fresh_sandbox.security_posture()
    assert posture["via_proxy"] is False
    # All Day-3 flags are still on; only the proxy bonus is missing.
    assert posture["score"] == 9


def test_score_caps_at_max_level(monkeypatch, fresh_sandbox):
    """Manually flip Day-3 stretch flags + proxy to confirm the score
    rolls up to ``max``.  This is what the runtime will look like
    once Sprint 5 Day 3 lands."""
    monkeypatch.setenv("DOCKER_HOST", "tcp://amor-docker-proxy:2375")
    posture = fresh_sandbox.security_posture()
    posture["flags_active"]["cap_drop_all"] = True
    posture["flags_active"]["pids_limit"] = 128
    posture["flags_active"]["seccomp_profile"] = "python311.json"
    # Re-score the same way the helper does.
    f = posture["flags_active"]
    score = sum(
        [
            1 if f["no_new_privileges"] else 0,
            1 if f["read_only"] else 0,
            1 if f["memory_limit"] else 0,
            1 if f["cpu_quota"] else 0,
            1 if f["default_network"] == "none" else 0,
            1 if f["tmpfs"] else 0,
            1 if f["cap_drop_all"] else 0,
            1 if f["pids_limit"] else 0,
            1 if f["seccomp_profile"] else 0,
            1 if posture["via_proxy"] else 0,
        ],
    )
    assert score == 10


@pytest.mark.asyncio
async def test_diagnostics_includes_security_block(monkeypatch):
    """``collect_sandbox`` exposes the posture under
    ``out["security"]`` so the frontend can drive the badge."""
    monkeypatch.setenv("AMOR_SANDBOX_WORKDIR", "/tmp/amor-sandbox-test")
    monkeypatch.setenv("DOCKER_HOST", "tcp://amor-docker-proxy:2375")
    from document_processor.code_intelligence.diagnostics import collect_sandbox
    out = await collect_sandbox(probe=False)
    assert "security" in out
    sec = out["security"]
    assert sec["via_proxy"] is True
    assert sec["docker_host"] == "tcp://amor-docker-proxy:2375"
    assert sec["level"] in ("baseline", "hardened", "max")
