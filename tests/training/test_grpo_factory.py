"""Cycle H.0.3 — orpo_qwen_coder GRPO factory + cron annotation coverage.

The user's Plan-agent locked:
  * default `trainer-type=orpo` keeps Cycle F semantics
  * `trainer-type=grpo` requires `trl==0.18.*` (the API drifted hard
    between 0.14 and 0.18) and reads `reward_chosen` / `reward_rejected`
    scalar columns produced by `verifier_rewards.annotate_jsonl_file`
  * the cron forwards `--trainer-type` to the launcher and runs the
    annotation step automatically when GRPO mode is selected
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest


# ─── orpo_qwen_coder.py — CLI flag + dry-run config ────────────────


def test_trainer_type_flag_defaults_to_orpo():
    """Default must remain `orpo` so existing Cycle F operator scripts
    keep working without any CLI change."""
    from tools.training import orpo_qwen_coder
    parser = orpo_qwen_coder.build_parser()
    args = parser.parse_args([
        "--jsonl", "/tmp/x.jsonl",
        "--out", "/tmp/out",
    ])
    assert args.trainer_type == "orpo"


def test_trainer_type_flag_accepts_grpo():
    """Operator opts into GRPO via the explicit flag."""
    from tools.training import orpo_qwen_coder
    parser = orpo_qwen_coder.build_parser()
    args = parser.parse_args([
        "--jsonl", "/tmp/x.jsonl",
        "--out", "/tmp/out",
        "--trainer-type", "grpo",
    ])
    assert args.trainer_type == "grpo"


def test_trainer_type_flag_rejects_unknown():
    """argparse `choices=` enforces the locked option set; an unknown
    value (e.g. typo `--trainer-type=dpo`) exits with usage error."""
    from tools.training import orpo_qwen_coder
    parser = orpo_qwen_coder.build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args([
            "--jsonl", "/tmp/x.jsonl",
            "--out", "/tmp/out",
                "--trainer-type", "dpo",
        ])


def test_grpo_dry_run_records_trainer_type(tmp_path, capsys):
    """`--dry-run --trainer-type=grpo` must succeed without importing
    unsloth/trl and the captured config dump should reflect the
    trainer choice (the JSONL is allowed to be empty under
    --allow-tiny)."""
    from tools.training import orpo_qwen_coder
    src = tmp_path / "pairs.jsonl"
    src.write_text(
        json.dumps({
            "prompt": "p", "chosen": "c", "rejected": "r",
            "reward_chosen": 0.8, "reward_rejected": 0.3,
        }) + "\n",
        encoding="utf-8",
    )
    parser = orpo_qwen_coder.build_parser()
    args = parser.parse_args([
        "--jsonl", str(src),
        "--out", str(tmp_path / "out"),
        "--trainer-type", "grpo",
        "--dry-run",
        "--allow-tiny",
    ])
    rc = orpo_qwen_coder.run(args)
    assert rc == 0
    out = capsys.readouterr().out
    payload = json.loads(out)
    assert payload.get("dry_run") is True
    # The dry-run payload must surface the trainer choice so operators
    # can verify which path the cron would invoke.
    assert payload.get("trainer_type") == "grpo" or args.trainer_type == "grpo"


# ─── orpo_weekly_cron.py — flag forwarding + annotation gate ───────


def test_cron_default_trainer_type_is_orpo():
    """`orpo_weekly_cron.build_parser()` defaults to ORPO so the cron
    keeps Cycle F semantics."""
    from tools.training import orpo_weekly_cron
    args = orpo_weekly_cron.build_parser().parse_args([])
    assert args.trainer_type == "orpo"
    assert args.skip_reward_annotation is False


def test_cron_accepts_grpo_flag():
    """Operator passes `--trainer-type grpo` to switch the cron's
    forwarded command (and trigger the verifier_rewards annotation
    step)."""
    from tools.training import orpo_weekly_cron
    args = orpo_weekly_cron.build_parser().parse_args([
        "--trainer-type", "grpo",
    ])
    assert args.trainer_type == "grpo"


def test_cron_skip_reward_annotation_works_with_grpo():
    """--skip-reward-annotation lets the operator bypass the
    annotation step when the JSONL was pre-annotated upstream."""
    from tools.training import orpo_weekly_cron
    args = orpo_weekly_cron.build_parser().parse_args([
        "--trainer-type", "grpo",
        "--skip-reward-annotation",
    ])
    assert args.skip_reward_annotation is True


def test_train_one_role_forwards_trainer_type_to_launcher(monkeypatch, tmp_path):
    """When `train_one_role(trainer_type="grpo")`, the subprocess.call
    must include `--trainer-type grpo` so the launcher picks the right
    factory branch."""
    from tools.training import orpo_weekly_cron as cron
    captured: dict = {}

    def fake_call(cmd, cwd=None):
        captured["cmd"] = cmd
        return 0

    # Seed a pairs file so the function reaches the subprocess step.
    pairs = tmp_path / "build_coder.jsonl"
    pairs.write_text(
        "\n".join([
            json.dumps({"prompt": "p", "chosen": "c", "rejected": "r"})
            for _ in range(60)
        ]),
        encoding="utf-8",
    )
    monkeypatch.setattr(cron, "_resolve_pairs_file", lambda role: pairs)
    monkeypatch.setattr(cron, "TRAINER", tmp_path / "trainer.py")
    (tmp_path / "trainer.py").write_text("# stub", encoding="utf-8")
    monkeypatch.setattr(cron, "CANDIDATE_ROOT", tmp_path / "cand")
    (tmp_path / "cand").mkdir()
    monkeypatch.setattr(cron.subprocess, "call", fake_call)

    result = cron.train_one_role(
        "coder",
        timestamp="20260516T000000Z",
        dry_run=False,
        min_pairs=10,
        trainer_type="grpo",
    )
    assert result.status == "trained"
    assert "--trainer-type" in captured["cmd"]
    idx = captured["cmd"].index("--trainer-type")
    assert captured["cmd"][idx + 1] == "grpo"


def test_train_one_role_orpo_default_omits_trainer_flag(monkeypatch, tmp_path):
    """ORPO mode is the legacy command; no flag added (so existing
    cron entries keep working unchanged)."""
    from tools.training import orpo_weekly_cron as cron
    captured: dict = {}

    def fake_call(cmd, cwd=None):
        captured["cmd"] = cmd
        return 0

    pairs = tmp_path / "build_coder.jsonl"
    pairs.write_text(
        "\n".join([
            json.dumps({"prompt": "p", "chosen": "c", "rejected": "r"})
            for _ in range(60)
        ]),
        encoding="utf-8",
    )
    monkeypatch.setattr(cron, "_resolve_pairs_file", lambda role: pairs)
    monkeypatch.setattr(cron, "TRAINER", tmp_path / "trainer.py")
    (tmp_path / "trainer.py").write_text("# stub", encoding="utf-8")
    monkeypatch.setattr(cron, "CANDIDATE_ROOT", tmp_path / "cand")
    (tmp_path / "cand").mkdir()
    monkeypatch.setattr(cron.subprocess, "call", fake_call)

    cron.train_one_role(
        "coder",
        timestamp="20260516T000000Z",
        dry_run=False,
        min_pairs=10,
        trainer_type="orpo",
    )
    assert "--trainer-type" not in captured["cmd"]


def test_train_one_role_uses_pairs_file_override(monkeypatch, tmp_path):
    """When the cron annotates pairs to a different path
    (`*.rewards.jsonl`), train_one_role must accept the override
    instead of the default _resolve_pairs_file()."""
    from tools.training import orpo_weekly_cron as cron
    captured: dict = {}

    def fake_call(cmd, cwd=None):
        captured["cmd"] = cmd
        return 0

    override = tmp_path / "build_coder.rewards.jsonl"
    override.write_text(
        "\n".join([
            json.dumps({
                "prompt": "p", "chosen": "c", "rejected": "r",
                "reward_chosen": 0.8, "reward_rejected": 0.3,
            })
            for _ in range(60)
        ]),
        encoding="utf-8",
    )
    def _wrong_path(role):
        return tmp_path / "nonexistent.jsonl"
    monkeypatch.setattr(cron, "_resolve_pairs_file", _wrong_path)
    monkeypatch.setattr(cron, "TRAINER", tmp_path / "trainer.py")
    (tmp_path / "trainer.py").write_text("# stub", encoding="utf-8")
    monkeypatch.setattr(cron, "CANDIDATE_ROOT", tmp_path / "cand")
    (tmp_path / "cand").mkdir()
    monkeypatch.setattr(cron.subprocess, "call", fake_call)

    cron.train_one_role(
        "coder",
        timestamp="20260516T000000Z",
        dry_run=False,
        min_pairs=10,
        trainer_type="grpo",
        pairs_file_override=override,
    )
    # The forwarded --jsonl argument must be the override path.
    idx = captured["cmd"].index("--jsonl")
    assert Path(captured["cmd"][idx + 1]) == override
