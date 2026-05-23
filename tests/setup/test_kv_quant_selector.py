"""
Cycle F Sprint 1 — tests for tools/llamaswap/select_kv_quant.py.

Exercises swap / rollback / status against a synthetic compose
directory; no real llama-swap container required.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent


@pytest.fixture
def selector_module(tmp_path, monkeypatch):
    """Import select_kv_quant.py with REPO_ROOT pointed at a tmp dir."""

    src = REPO_ROOT / "tools" / "llamaswap" / "select_kv_quant.py"
    assert src.is_file(), f"select_kv_quant.py missing: {src}"

    # Mirror the compose/llama-swap layout under tmp_path.
    compose_dir = tmp_path / "compose" / "llama-swap"
    compose_dir.mkdir(parents=True)
    (compose_dir / "config.q4_0.yaml").write_text("q4 content\n", encoding="utf-8")
    (compose_dir / "config.q8_0.yaml").write_text("q8 content\n", encoding="utf-8")

    # Load the module via spec so we can override its REPO_ROOT.
    spec = importlib.util.spec_from_file_location("select_kv_quant_test", src)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["select_kv_quant_test"] = mod
    spec.loader.exec_module(mod)

    # Override constants so the module reads/writes under tmp_path.
    mod.REPO_ROOT = tmp_path
    mod.COMPOSE_DIR = compose_dir
    mod.ACTIVE = compose_dir / "config.yaml"
    mod.PREV = compose_dir / "config.prev.yaml"

    return mod, compose_dir


def test_swap_writes_active_pointing_at_q4(selector_module):
    mod, compose_dir = selector_module
    rc = mod.swap("q4_0")
    assert rc == 0
    active_body = (compose_dir / "config.yaml").read_text(encoding="utf-8")
    assert active_body == "q4 content\n"


def test_swap_writes_active_pointing_at_q8(selector_module):
    mod, compose_dir = selector_module
    rc = mod.swap("q8_0")
    assert rc == 0
    active_body = (compose_dir / "config.yaml").read_text(encoding="utf-8")
    assert active_body == "q8 content\n"


def test_swap_backs_up_previous(selector_module):
    mod, compose_dir = selector_module
    # First swap: q4
    assert mod.swap("q4_0") == 0
    # Second swap: q8 — prev should now be the q4 content
    assert mod.swap("q8_0") == 0
    prev_body = (compose_dir / "config.prev.yaml").read_text(encoding="utf-8")
    assert prev_body == "q4 content\n"


def test_swap_idempotent(selector_module):
    mod, compose_dir = selector_module
    assert mod.swap("q4_0") == 0
    # Capture mtime; second call should not rewrite the file unnecessarily.
    mtime_first = (compose_dir / "config.yaml").stat().st_mtime
    assert mod.swap("q4_0") == 0
    # The function correctly detects "already pointing at variant" and
    # returns 0 without touching the file.
    mtime_second = (compose_dir / "config.yaml").stat().st_mtime
    assert mtime_first == mtime_second


def test_rollback_restores_previous(selector_module):
    mod, compose_dir = selector_module
    assert mod.swap("q4_0") == 0
    assert mod.swap("q8_0") == 0
    assert (compose_dir / "config.yaml").read_text() == "q8 content\n"
    assert mod.rollback() == 0
    assert (compose_dir / "config.yaml").read_text() == "q4 content\n"


def test_rollback_without_previous_fails(selector_module):
    mod, _ = selector_module
    rc = mod.rollback()
    assert rc == 1


def test_unknown_quant_rejected(selector_module):
    mod, _ = selector_module
    rc = mod.swap("q2_0")
    assert rc == 1


def test_missing_variant_file_rejected(selector_module):
    mod, compose_dir = selector_module
    (compose_dir / "config.q4_0.yaml").unlink()
    rc = mod.swap("q4_0")
    assert rc == 2


def test_status_reports_current_variant(selector_module, capsys):
    mod, compose_dir = selector_module
    mod.swap("q8_0")
    rc = mod.status()
    assert rc == 0
    captured = capsys.readouterr().out
    assert "q8_0" in captured
