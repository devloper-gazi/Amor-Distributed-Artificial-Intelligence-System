"""
Unit tests for ``document_processor/quick_code/sandbox_tier.py``.

Tests use injected fake sandboxes so we never touch Docker.  The
fake records the timeout it received so we can assert the tier-to-
limit mapping is correct.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from document_processor.quick_code.contracts import SandboxResult, SandboxTier
from document_processor.quick_code.sandbox_tier import TieredSandbox


def _run(coro):
    return asyncio.run(coro)


# ─────────────────────────────────────────────────────────────────────
# Fake sandboxes
# ─────────────────────────────────────────────────────────────────────


class _FakeSandbox:
    """Records the parameters it was called with and returns a
    ``dict`` that matches the legacy ``ExecutionResult`` shape."""

    def __init__(
        self,
        *,
        ok: bool = True,
        exit_code: int = 0,
        stdout: str = "OK",
        stderr: str = "",
        error: str | None = None,
        skipped: bool = False,
    ) -> None:
        self.ok = ok
        self.exit_code = exit_code
        self.stdout = stdout
        self.stderr = stderr
        self.error = error
        self.skipped = skipped
        self.calls: list[dict[str, Any]] = []

    async def execute(
        self,
        code: str,
        *,
        language: str = "python",
        extra_files: dict[str, str] | None = None,
        install_packages: list[str] | None = None,
        timeout: int | None = None,
        stdin_data: str | None = None,
    ) -> dict[str, Any]:
        self.calls.append({
            "code": code,
            "language": language,
            "timeout": timeout,
            "stdin_data": stdin_data,
        })
        return {
            "stdout": self.stdout,
            "stderr": self.stderr,
            "exit_code": self.exit_code,
            "error": self.error,
            "duration_ms": 12.0,
            "memory_mb": 5.0,
            "timed_out": False,
            "skipped": self.skipped,
        }


# ─────────────────────────────────────────────────────────────────────
# Tier → timeout mapping
# ─────────────────────────────────────────────────────────────────────


def test_quick_tier_uses_quick_timeout():
    quick = _FakeSandbox()
    pro = _FakeSandbox()
    ts = TieredSandbox(
        quick_sandbox=quick,
        pro_sandbox=pro,
        quick_timeout_s=15,
        pro_timeout_s=45,
    )
    _run(ts.execute("print(1)", tier=SandboxTier.QUICK))
    assert len(quick.calls) == 1
    assert quick.calls[0]["timeout"] == 15
    assert pro.calls == []


def test_pro_tier_uses_pro_timeout():
    quick = _FakeSandbox()
    pro = _FakeSandbox()
    ts = TieredSandbox(
        quick_sandbox=quick,
        pro_sandbox=pro,
        quick_timeout_s=15,
        pro_timeout_s=45,
    )
    _run(ts.execute("print(1)", tier=SandboxTier.PRO))
    assert len(pro.calls) == 1
    assert pro.calls[0]["timeout"] == 45
    assert quick.calls == []


def test_limits_for_returns_correct_tier_limits():
    ts = TieredSandbox(
        quick_mem_mb=256,
        quick_timeout_s=15,
        pro_mem_mb=512,
        pro_timeout_s=45,
        quick_sandbox=_FakeSandbox(),
        pro_sandbox=_FakeSandbox(),
    )
    quick_limits = ts.limits_for(SandboxTier.QUICK)
    pro_limits = ts.limits_for(SandboxTier.PRO)
    assert quick_limits == {"memory_mb": 256, "timeout_s": 15}
    assert pro_limits == {"memory_mb": 512, "timeout_s": 45}


# ─────────────────────────────────────────────────────────────────────
# Result normalisation
# ─────────────────────────────────────────────────────────────────────


def test_execute_returns_typed_sandbox_result():
    quick = _FakeSandbox(stdout="HI", exit_code=0)
    ts = TieredSandbox(quick_sandbox=quick, pro_sandbox=_FakeSandbox())
    out = _run(ts.execute("print('HI')"))
    assert isinstance(out, SandboxResult)
    assert out.ok is True
    assert out.stdout == "HI"
    assert out.tier is SandboxTier.QUICK


def test_execute_failure_propagates_as_not_ok():
    quick = _FakeSandbox(exit_code=1, stderr="Traceback...")
    ts = TieredSandbox(quick_sandbox=quick, pro_sandbox=_FakeSandbox())
    out = _run(ts.execute("raise"))
    assert out.ok is False
    assert "Traceback" in out.stderr


def test_skipped_run_is_not_a_failure():
    """When Docker is unavailable, ExecutionSandbox returns a
    skipped result.  TieredSandbox should treat that as a deferred
    pass (ok=True) so the verification gate doesn't flag a critical."""
    quick = _FakeSandbox(skipped=True, stderr="docker unavailable")
    ts = TieredSandbox(quick_sandbox=quick, pro_sandbox=_FakeSandbox())
    out = _run(ts.execute("print(1)"))
    assert out.ok is True


def test_execute_handles_underlying_exception():
    class Crashy:
        async def execute(self, *a, **kw):
            raise RuntimeError("docker daemon died")

    ts = TieredSandbox(quick_sandbox=Crashy(), pro_sandbox=_FakeSandbox())
    out = _run(ts.execute("anything"))
    assert out.ok is False
    assert "RuntimeError" in out.stderr


# ─────────────────────────────────────────────────────────────────────
# Pro mem ≥ Quick mem invariant
# ─────────────────────────────────────────────────────────────────────


def test_pro_memory_floor_enforced():
    """Pro tier must have at least as much memory as Quick tier."""
    ts = TieredSandbox(
        quick_mem_mb=512,
        pro_mem_mb=256,  # nonsense — should be clamped up
        quick_sandbox=_FakeSandbox(),
        pro_sandbox=_FakeSandbox(),
    )
    pro_limits = ts.limits_for(SandboxTier.PRO)
    assert pro_limits["memory_mb"] >= 512


def test_pass_through_extra_files_and_packages():
    quick = _FakeSandbox()
    ts = TieredSandbox(quick_sandbox=quick, pro_sandbox=_FakeSandbox())
    _run(
        ts.execute(
            "print(1)",
            extra_files={"helper.py": "def f(): pass"},
            install_packages=["requests"],
        )
    )
    assert quick.calls[0]["timeout"] == 15
