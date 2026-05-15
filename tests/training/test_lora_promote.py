"""Cycle F Sprint 6 piece 3 — tests for tools/lora/promote.py."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parent.parent.parent


@pytest.fixture
def promote_module():
    src = REPO_ROOT / "tools" / "lora" / "promote.py"
    assert src.is_file()
    spec = importlib.util.spec_from_file_location("lora_promote_test", src)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["lora_promote_test"] = mod
    spec.loader.exec_module(mod)
    return mod


# ─── promote() ──────────────────────────────────────────────────────


def test_promote_unknown_role_rejected(promote_module, tmp_path):
    rc = promote_module.promote(
        "architect", tmp_path / "x.gguf",
    )
    assert rc == 1


def test_promote_missing_candidate_rejected(promote_module, tmp_path, monkeypatch):
    monkeypatch.setattr(promote_module, "LORA_ROOT", tmp_path)
    rc = promote_module.promote(
        "coder", tmp_path / "nonexistent.gguf",
    )
    assert rc == 1


def test_promote_first_time_creates_active_slot(
    promote_module, tmp_path, monkeypatch,
):
    monkeypatch.setattr(promote_module, "LORA_ROOT", tmp_path)
    candidate = tmp_path / "candidate.gguf"
    candidate.write_bytes(b"new adapter bytes")

    rc = promote_module.promote("coder", candidate)
    assert rc == 0
    active = tmp_path / "coder-r16.gguf"
    assert active.is_file()
    assert active.read_bytes() == b"new adapter bytes"
    # No previous to back up.
    prev = tmp_path / "coder-r16.prev.gguf"
    assert not prev.is_file()


def test_promote_existing_is_backed_up_to_prev(
    promote_module, tmp_path, monkeypatch,
):
    monkeypatch.setattr(promote_module, "LORA_ROOT", tmp_path)
    # Pre-existing in-production adapter
    active = tmp_path / "coder-r16.gguf"
    active.write_bytes(b"old adapter")
    candidate = tmp_path / "candidate.gguf"
    candidate.write_bytes(b"new adapter")

    rc = promote_module.promote("coder", candidate)
    assert rc == 0
    prev = tmp_path / "coder-r16.prev.gguf"
    assert prev.is_file()
    assert prev.read_bytes() == b"old adapter"
    assert active.read_bytes() == b"new adapter"


# ─── rollback() ────────────────────────────────────────────────────


def test_rollback_unknown_role_rejected(promote_module, tmp_path):
    rc = promote_module.rollback("architect")
    assert rc == 1


def test_rollback_without_prev_rejected(promote_module, tmp_path, monkeypatch):
    monkeypatch.setattr(promote_module, "LORA_ROOT", tmp_path)
    rc = promote_module.rollback("coder")
    assert rc == 1


def test_rollback_restores_prev_to_active(
    promote_module, tmp_path, monkeypatch,
):
    monkeypatch.setattr(promote_module, "LORA_ROOT", tmp_path)
    (tmp_path / "coder-r16.gguf").write_bytes(b"new")
    (tmp_path / "coder-r16.prev.gguf").write_bytes(b"old")

    rc = promote_module.rollback("coder")
    assert rc == 0
    assert (tmp_path / "coder-r16.gguf").read_bytes() == b"old"
    # prev is consumed.
    assert not (tmp_path / "coder-r16.prev.gguf").is_file()


# ─── status() ──────────────────────────────────────────────────────


def test_status_runs_without_error_on_empty_tree(
    promote_module, tmp_path, monkeypatch, capsys,
):
    monkeypatch.setattr(promote_module, "LORA_ROOT", tmp_path)
    rc = promote_module.status()
    assert rc == 0
    out = capsys.readouterr().out
    assert "LoRA root" in out
    for role in promote_module.VALID_ROLES:
        assert role in out


def test_status_after_promote_shows_active(
    promote_module, tmp_path, monkeypatch, capsys,
):
    monkeypatch.setattr(promote_module, "LORA_ROOT", tmp_path)
    candidate = tmp_path / "candidate.gguf"
    candidate.write_bytes(b"x")
    promote_module.promote("coder", candidate)
    promote_module.status()
    out = capsys.readouterr().out
    assert "coder-r16.gguf" in out


# ─── Role constants ────────────────────────────────────────────────


def test_valid_roles_match_orpo_role_adapter_ids(promote_module):
    # Both files MUST use the same role names; otherwise promote
    # routes to a slot orpo_role_adapter.py would never write.
    assert set(promote_module.VALID_ROLES) == {"coder", "tester", "debugger"}
