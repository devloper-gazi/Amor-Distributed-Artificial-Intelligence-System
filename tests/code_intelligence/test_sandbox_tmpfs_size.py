"""v18.1.2 hotfix — sandbox tmpfs ceiling regression coverage.

Reproduces the 5/5/2026 HumanEval+ failure mode where 6 consecutive
runs scored pass@1=0.0 with case-level errors of the form
``ERROR: Could not install packages due to an OSError:
[Errno 28] No space left on device``.  Root cause: the 384m default
tmpfs at /tmp ran out of room when pip's wheel-staging area and the
``--target=/tmp/pip-prefix`` install destination both wrote there
during ``numpy`` install (~75 MB final + ~50-100 MB transient).

Fix: bump default to 768m via tunable setting + env override.
"""

from __future__ import annotations

import pytest

from document_processor.code_intelligence import sandbox


def test_tmpfs_size_mb_default(monkeypatch):
    """When settings + env both absent/empty, returns the 768 default."""
    monkeypatch.delenv("AMOR_CODE_SANDBOX_TMPFS_SIZE_MB", raising=False)
    from document_processor.config.settings import settings as _settings
    monkeypatch.setattr(_settings, "code_sandbox_tmpfs_size_mb", 768, raising=False)
    assert sandbox._tmpfs_size_mb() == 768


def test_tmpfs_size_mb_env_override(monkeypatch):
    """Env var takes precedence over settings — operator on tight
    RAM can downsize to 256m without touching code."""
    monkeypatch.setenv("AMOR_CODE_SANDBOX_TMPFS_SIZE_MB", "256")
    assert sandbox._tmpfs_size_mb() == 256


def test_tmpfs_size_mb_settings_value(monkeypatch):
    monkeypatch.delenv("AMOR_CODE_SANDBOX_TMPFS_SIZE_MB", raising=False)
    from document_processor.config.settings import settings as _settings
    monkeypatch.setattr(_settings, "code_sandbox_tmpfs_size_mb", 1024, raising=False)
    assert sandbox._tmpfs_size_mb() == 1024


def test_tmpfs_size_mb_floor_clamps_dangerous_low(monkeypatch):
    """Below 128m even import of standard libs ENOSPCs — clamp up."""
    monkeypatch.setenv("AMOR_CODE_SANDBOX_TMPFS_SIZE_MB", "32")
    assert sandbox._tmpfs_size_mb() == 128


def test_tmpfs_size_mb_ceiling_clamps_dangerous_high(monkeypatch):
    """4096m ceiling — operator typo of 40960 doesn't accidentally
    chew the host's RAM (tmpfs is RAM-backed)."""
    monkeypatch.setenv("AMOR_CODE_SANDBOX_TMPFS_SIZE_MB", "40960")
    assert sandbox._tmpfs_size_mb() == 4096


def test_tmpfs_size_mb_unparseable_env_falls_to_settings(monkeypatch):
    """A bogus env value (`AMOR_CODE_SANDBOX_TMPFS_SIZE_MB=foo`)
    must not crash the sandbox — fall through to settings."""
    monkeypatch.setenv("AMOR_CODE_SANDBOX_TMPFS_SIZE_MB", "not-a-number")
    from document_processor.config.settings import settings as _settings
    monkeypatch.setattr(_settings, "code_sandbox_tmpfs_size_mb", 512, raising=False)
    assert sandbox._tmpfs_size_mb() == 512


def test_security_posture_reports_actual_tmpfs(monkeypatch):
    """The /admin/llm + diagnostics surfaces read security_posture();
    the reported tmpfs value MUST match what the runtime --tmpfs arg
    actually requests, otherwise operators see stale 384m forever."""
    monkeypatch.setenv("AMOR_CODE_SANDBOX_TMPFS_SIZE_MB", "1024")
    sb = sandbox.ExecutionSandbox()
    posture = sb.security_posture()
    assert "1024m" in posture["flags_active"]["tmpfs"], (
        f"security_posture tmpfs={posture['flags_active']['tmpfs']!r} "
        "didn't reflect the env override — flag-vs-runtime drift"
    )


def test_default_post_v18_1_2_is_768m():
    """Lock the new default at 768m so a future commit that lowers it
    back to 384m breaks the test loudly (HumanEval+ regression
    surface)."""
    from document_processor.config.settings import settings as _settings
    assert _settings.code_sandbox_tmpfs_size_mb >= 512, (
        "v18.1.2 raised the sandbox tmpfs default to 768m to fix the "
        "HumanEval+ ENOSPC bug.  Lowering below 512m re-opens that bug; "
        "do not regress without testing pip install --target=/tmp/pip-prefix "
        "numpy under load."
    )
