"""Cycle F Sprint 3 — tests for tools/training/orpo_role_adapter.py.

Exercises CLI surface, role -> path resolution, min-pairs gate, and
the dry-run exit code.  Does NOT invoke the actual Unsloth trainer
(that needs GPU + ~30 GB disk).
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parent.parent.parent


@pytest.fixture
def orpo_module():
    """Import the script as a module without invoking __main__."""

    src = REPO_ROOT / "tools" / "training" / "orpo_role_adapter.py"
    assert src.is_file()
    spec = importlib.util.spec_from_file_location("orpo_role_adapter_test", src)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["orpo_role_adapter_test"] = mod
    spec.loader.exec_module(mod)
    return mod


# ─── ROLE_ADAPTER_IDS invariants ────────────────────────────────────


def test_role_adapter_ids_are_unique(orpo_module):
    ids = list(orpo_module.ROLE_ADAPTER_IDS.values())
    assert len(ids) == len(set(ids)), "Adapter IDs must be unique"


def test_role_adapter_ids_start_at_zero(orpo_module):
    """llama.cpp PR #10994 assigns IDs in mount order starting at 0."""

    ids = sorted(orpo_module.ROLE_ADAPTER_IDS.values())
    assert ids == list(range(len(ids)))


def test_role_adapter_ids_cover_three_roles(orpo_module):
    expected = {"coder", "tester", "debugger"}
    assert set(orpo_module.ROLE_ADAPTER_IDS.keys()) == expected


# ─── argparse surface ──────────────────────────────────────────────


def test_parser_requires_role(orpo_module):
    parser = orpo_module.build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args([])


def test_parser_rejects_unknown_role(orpo_module):
    parser = orpo_module.build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["--role", "architect"])


def test_parser_accepts_coder_role(orpo_module):
    parser = orpo_module.build_parser()
    args = parser.parse_args(["--role", "coder"])
    assert args.role == "coder"


def test_parser_defaults_match_cycle_f_recipe(orpo_module):
    """r=16, alpha=32 (rsLoRA sweet spot per the plan file)."""

    parser = orpo_module.build_parser()
    args = parser.parse_args(["--role", "coder"])
    assert args.lora_r == 16
    assert args.lora_alpha == 32
    assert args.lora_dropout == 0.05
    assert args.lr == 8e-6
    assert args.beta == 0.1
    assert args.epochs == 1.0
    assert args.max_seq_length == 2048


# ─── resolve_paths ─────────────────────────────────────────────────


def test_resolve_paths_uses_defaults_when_unset(orpo_module):
    parser = orpo_module.build_parser()
    args = parser.parse_args(["--role", "tester"])
    jsonl, out = orpo_module.resolve_paths(args)
    assert jsonl.name == "tester.jsonl"
    assert jsonl.parent.name == "preference_pairs"
    assert out.name == "tester-r16"
    assert out.parent.name == "lora"


def test_resolve_paths_honors_explicit_overrides(orpo_module, tmp_path):
    parser = orpo_module.build_parser()
    args = parser.parse_args([
        "--role", "coder",
        "--jsonl", str(tmp_path / "custom.jsonl"),
        "--out", str(tmp_path / "out"),
    ])
    jsonl, out = orpo_module.resolve_paths(args)
    assert jsonl == tmp_path / "custom.jsonl"
    assert out == tmp_path / "out"


def test_resolve_paths_uses_lora_r_in_default_out(orpo_module):
    parser = orpo_module.build_parser()
    args = parser.parse_args(["--role", "debugger", "--r", "8"])
    _, out = orpo_module.resolve_paths(args)
    assert out.name == "debugger-r8"


# ─── main() — dry-run exit codes ────────────────────────────────────


def _write_jsonl(path: Path, n_pairs: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        for i in range(n_pairs):
            f.write(json.dumps({
                "prompt": f"q{i}",
                "chosen": f"a{i}",
                "rejected": f"b{i}",
            }) + "\n")


def test_main_dry_run_succeeds_with_real_corpus(orpo_module, tmp_path, monkeypatch, capsys):
    jsonl = tmp_path / "coder.jsonl"
    _write_jsonl(jsonl, 100)
    monkeypatch.setattr(
        sys, "argv",
        ["orpo_role_adapter.py", "--role", "coder", "--jsonl", str(jsonl),
         "--out", str(tmp_path / "out"), "--dry-run"],
    )
    rc = orpo_module.main()
    assert rc == 0


def test_main_fails_when_corpus_missing(orpo_module, tmp_path, monkeypatch):
    monkeypatch.setattr(
        sys, "argv",
        ["orpo_role_adapter.py", "--role", "coder",
         "--jsonl", str(tmp_path / "missing.jsonl"),
         "--out", str(tmp_path / "out"), "--dry-run"],
    )
    rc = orpo_module.main()
    assert rc == 2


def test_main_fails_when_pairs_below_min(orpo_module, tmp_path, monkeypatch):
    jsonl = tmp_path / "coder.jsonl"
    _write_jsonl(jsonl, 3)  # well below DEFAULT_MIN_PAIRS=50
    monkeypatch.setattr(
        sys, "argv",
        ["orpo_role_adapter.py", "--role", "coder",
         "--jsonl", str(jsonl), "--out", str(tmp_path / "out"),
         "--dry-run"],
    )
    rc = orpo_module.main()
    assert rc == 2


def test_main_allow_tiny_bypasses_min_pairs(orpo_module, tmp_path, monkeypatch):
    jsonl = tmp_path / "coder.jsonl"
    _write_jsonl(jsonl, 3)
    monkeypatch.setattr(
        sys, "argv",
        ["orpo_role_adapter.py", "--role", "coder",
         "--jsonl", str(jsonl), "--out", str(tmp_path / "out"),
         "--allow-tiny", "--dry-run"],
    )
    rc = orpo_module.main()
    assert rc == 0
