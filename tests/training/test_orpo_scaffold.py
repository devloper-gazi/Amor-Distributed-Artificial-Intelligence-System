"""
Cycle C Sprint 6 Day 2 — ORPO scaffold tests.

The trainer + converter are GPU-bound, so we don't actually run them
here.  Instead we drive the scaffolds through their dry-run paths
and assert:

* JSONL loader parses good rows / rejects bad ones.
* ORPOConfig args reflect the plan exactly (lr, batch, beta, ...).
* Converter resolves ``convert-lora-to-gguf.py`` from candidate paths
  and produces the right command.
* ``--min-pairs`` gate refuses small JSONLs unless ``--allow-tiny``.
"""

from __future__ import annotations

import json
import sys
import textwrap
from pathlib import Path

import pytest

# Make ``tools.training`` importable when pytest is run from /app.
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from tools.training import orpo_qwen_coder, convert_lora_gguf, export_pairs_jsonl


# ─── orpo_qwen_coder ───────────────────────────────────────────────


def test_load_jsonl_parses_valid(tmp_path: Path):
    p = tmp_path / "tiny.jsonl"
    p.write_text(
        "\n".join(
            [
                json.dumps({"prompt": "p1", "chosen": "c1", "rejected": "r1"}),
                json.dumps({"prompt": "p2", "chosen": "c2", "rejected": "r2"}),
            ],
        ),
        encoding="utf-8",
    )
    rows = orpo_qwen_coder.load_jsonl(p)
    assert len(rows) == 2
    assert rows[0]["prompt"] == "p1"


def test_load_jsonl_rejects_missing_field(tmp_path: Path):
    p = tmp_path / "bad.jsonl"
    p.write_text(json.dumps({"prompt": "p1"}) + "\n", encoding="utf-8")
    with pytest.raises(ValueError, match="missing field 'chosen'"):
        orpo_qwen_coder.load_jsonl(p)


def test_build_trainer_args_matches_cycle_c_plan():
    """The plan locks several values; this test pins them so a future
    drift surfaces in CI."""
    args = orpo_qwen_coder.build_parser().parse_args(
        ["--jsonl", "x.jsonl", "--out", "/tmp/out"],
    )
    cfg = orpo_qwen_coder.build_trainer_args(args)
    assert cfg["per_device_train_batch_size"] == 1
    assert cfg["gradient_accumulation_steps"] == 4  # effective bs=4
    assert cfg["learning_rate"] == 8e-6
    assert cfg["lr_scheduler_type"] == "cosine"
    assert cfg["optim"] == "paged_adamw_8bit"
    assert cfg["beta"] == 0.1
    assert cfg["max_length"] == 2048
    assert cfg["max_prompt_length"] == 512
    assert cfg["num_train_epochs"] == 1.0


def test_min_pairs_gate_blocks_tiny(tmp_path: Path, capsys, monkeypatch):
    p = tmp_path / "tiny.jsonl"
    p.write_text(
        json.dumps({"prompt": "p", "chosen": "c", "rejected": "r"}) + "\n",
        encoding="utf-8",
    )
    out = tmp_path / "out"
    monkeypatch.setattr(
        sys, "argv", ["orpo_qwen_coder.py", "--jsonl", str(p), "--out", str(out)],
    )
    rc = orpo_qwen_coder.main()
    assert rc == 2  # refused — only 1 pair, threshold 200


def test_dry_run_writes_planned_config(tmp_path: Path, monkeypatch):
    p = tmp_path / "tiny.jsonl"
    p.write_text(
        "\n".join(
            json.dumps({"prompt": f"p{i}", "chosen": f"c{i}", "rejected": f"r{i}"})
            for i in range(3)
        ),
        encoding="utf-8",
    )
    out = tmp_path / "out"
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "orpo_qwen_coder.py",
            "--jsonl", str(p),
            "--out", str(out),
            "--allow-tiny",
            "--dry-run",
        ],
    )
    rc = orpo_qwen_coder.main()
    assert rc == 0
    plan = json.loads((out / "planned_orpo_config.json").read_text())
    assert plan["pairs"] == 3
    assert plan["lora"]["r"] == 8
    assert plan["lora"]["alpha"] == 16
    assert "q_proj" in plan["lora"]["target_modules"]
    assert plan["config"]["max_length"] == 2048


# ─── convert_lora_gguf ────────────────────────────────────────────


def test_find_converter_walks_candidates(tmp_path: Path):
    """Resolver respects an explicit ``--llama-cpp`` path even when
    ``LLAMA_CPP_DIR`` was empty at import time."""
    fake_dir = tmp_path / "llama.cpp"
    fake_dir.mkdir()
    (fake_dir / "convert-lora-to-gguf.py").write_text("# stub\n")

    found = convert_lora_gguf.find_converter(str(fake_dir))
    assert found is not None
    assert found.name == "convert-lora-to-gguf.py"
    assert str(found).startswith(str(fake_dir))


def test_find_converter_walks_nested_convert_dir(tmp_path: Path):
    """llama.cpp moved the script under ``convert/`` in some forks —
    the resolver must find it there too."""
    fake_dir = tmp_path / "llama.cpp"
    (fake_dir / "convert").mkdir(parents=True)
    (fake_dir / "convert" / "convert-lora-to-gguf.py").write_text("# stub\n")

    found = convert_lora_gguf.find_converter(str(fake_dir))
    assert found is not None
    assert found.name == "convert-lora-to-gguf.py"


def test_find_converter_returns_none_when_missing(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("LLAMA_CPP_DIR", str(tmp_path / "doesnotexist"))
    monkeypatch.setattr(
        convert_lora_gguf,
        "CANDIDATE_LLAMA_CPP_DIRS",
        [str(tmp_path / "nope1"), str(tmp_path / "nope2")],
    )
    assert convert_lora_gguf.find_converter(None) is None


def test_dry_run_prints_command(tmp_path: Path, monkeypatch, capsys):
    peft = tmp_path / "peft"
    peft.mkdir()
    base = tmp_path / "base.gguf"
    base.write_bytes(b"GGUF" + b"\x00" * 16)
    fake_dir = tmp_path / "llama.cpp"
    fake_dir.mkdir()
    (fake_dir / "convert-lora-to-gguf.py").write_text("# stub\n")

    monkeypatch.setattr(
        sys,
        "argv",
        [
            "convert_lora_gguf.py",
            "--peft", str(peft),
            "--base", str(base),
            "--out", str(tmp_path / "out.gguf"),
            "--llama-cpp", str(fake_dir),
            "--dry-run",
        ],
    )
    rc = convert_lora_gguf.main()
    assert rc == 0
    out = capsys.readouterr().out
    assert "convert-lora-to-gguf.py" in out
    # Windows path-escape in JSON output uses `\\` literally; the raw
    # str(peft) here contains single `\`.  Compare via the JSON-escaped
    # form so the test works cross-platform (Linux paths use `/` and
    # match either way).
    import json as _json
    assert _json.dumps(str(peft)).strip('"') in out


def test_converter_errors_when_inputs_missing(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "convert_lora_gguf.py",
            "--peft", str(tmp_path / "missing"),
            "--base", str(tmp_path / "missing.gguf"),
            "--out", str(tmp_path / "out.gguf"),
        ],
    )
    rc = convert_lora_gguf.main()
    assert rc == 2  # missing PEFT dir


# ─── export_pairs_jsonl ───────────────────────────────────────────


def test_parse_since_relative():
    from datetime import timedelta
    a = export_pairs_jsonl._parse_since("30d")
    b = export_pairs_jsonl._parse_since("12h")
    c = export_pairs_jsonl._parse_since("all")
    assert a is not None
    assert b is not None
    assert c is None
    # 30d > 12h ago in monotonic-ish wall-clock order
    assert a < b


def test_parse_since_absolute():
    d = export_pairs_jsonl._parse_since("2026-01-01")
    assert d.year == 2026 and d.month == 1 and d.day == 1


def test_parse_since_rejects_garbage():
    import argparse
    with pytest.raises(argparse.ArgumentTypeError):
        export_pairs_jsonl._parse_since("yesterday")
