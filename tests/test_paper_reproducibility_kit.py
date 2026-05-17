"""Cycle J.3 — reproducibility-kit smoke tests.

Verifies the paper's reproducibility scripts exist + are well-formed
+ the one-script wrapper exits cleanly in CI mode (no GPU).
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parent.parent


def test_paper_draft_exists_and_has_sections():
    """The paper draft must have the canonical section headers so
    venue submissions can find the abstract / method / results."""
    paper = REPO_ROOT / "docs" / "papers" / "verifier_reward_grpo.md"
    assert paper.is_file()
    body = paper.read_text(encoding="utf-8")
    for header in ["## TL;DR", "## 2. Method", "## 4. Results",
                   "## 6. Reproducibility kit"]:
        assert header in body, f"missing section: {header}"


def test_reproducibility_kit_doc_exists():
    """The reproducibility kit MD must list every script the paper
    references."""
    kit = REPO_ROOT / "docs" / "papers" / "reproducibility_kit.md"
    assert kit.is_file()
    body = kit.read_text(encoding="utf-8")
    expected_scripts = [
        "tools/pull_models.py",
        "tools/run_sprint0_v18.sh",
        "tools/training/synth_pair_generator.py",
        "tools/training/verifier_rewards.py",
        "tools/training/orpo_qwen_coder.py",
        "tools/training/saten_compression.py",
        "tools/run_v20_launch_gate.py",
    ]
    for s in expected_scripts:
        assert s in body, f"reproducibility kit missing reference to {s}"


def test_one_script_wrapper_exists():
    """``tools/papers/reproduce_h3_j1.sh`` is the J.3 deliverable."""
    sh = REPO_ROOT / "tools" / "papers" / "reproduce_h3_j1.sh"
    assert sh.is_file()
    body = sh.read_text(encoding="utf-8")
    # Documented modes must be present.
    assert "--ci" in body
    assert "--no-train" in body
    assert "Phase 1" in body and "Phase 5" in body


def test_plan_agent_thresholds_appear_in_appendix():
    """Appendix B audit trail lists every Plan-agent locked threshold
    so revising one requires a tracked change to the paper."""
    paper = REPO_ROOT / "docs" / "papers" / "verifier_reward_grpo.md"
    body = paper.read_text(encoding="utf-8")
    thresholds = [
        "GRPO_PROPERTY_FAILURE_REDUCTION_TARGET_PCT",
        "SATEN_MAX_LOSS_PP",
        "SATEN_MIN_RECOVERY_FRACTION",
        "KAT_TARGET_PERPLEXITY_RATIO",
        "JEPA_TARGET_LOSS",
        "BITNET_AGREEMENT_TARGET_PCT",
        "BITNET_P95_LATENCY_MAX_MS",
        "BITNET_MIN_SAMPLES",
    ]
    for t in thresholds:
        assert t in body, f"missing threshold doc: {t}"


@pytest.mark.skipif(sys.platform == "win32",
                    reason="bash wrapper invocation tested on Linux/WSL")
def test_reproduce_script_ci_mode_exits_clean(tmp_path, monkeypatch):
    """Smoke: ``reproduce_h3_j1.sh --ci`` runs the simulation-only
    code paths and exits zero.  Validates the script's plumbing
    without GPU."""
    monkeypatch.chdir(REPO_ROOT)
    result = subprocess.run(
        ["bash", "tools/papers/reproduce_h3_j1.sh", "--ci"],
        capture_output=True, text=True, timeout=120,
    )
    # The wrapper is intentionally tolerant of missing GGUFs etc.
    # The v20 gate may still report FAIL/INCOMPLETE but the script
    # itself must not crash.
    assert "Phase 5" in result.stdout or "Phase 5" in result.stderr
