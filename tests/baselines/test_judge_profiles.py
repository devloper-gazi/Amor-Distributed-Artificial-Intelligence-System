"""
Cycle E v18 — judge profile loader tests.

The Sprint 0 v18 runner depends on tools/judge/judge_profiles.json
having a stable shape.  These tests guard the contract so a bad edit
doesn't surface only at 3 AM during the overnight run.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
PROFILES_PATH = REPO_ROOT / "tools" / "judge" / "judge_profiles.json"


@pytest.fixture(scope="module")
def profiles() -> dict:
    return json.loads(PROFILES_PATH.read_text(encoding="utf-8"))


# ─── Top-level shape ─────────────────────────────────────────────


def test_top_level_keys(profiles):
    assert "default" in profiles
    assert "profiles" in profiles
    assert "_protocol_version" in profiles
    assert isinstance(profiles["profiles"], dict)
    assert profiles["profiles"], "must have at least one profile"


def test_default_profile_exists(profiles):
    default = profiles["default"]
    assert default in profiles["profiles"], (
        f"default profile {default!r} is not defined in profiles dict"
    )


def test_default_is_mistral(profiles):
    # v18 default = Mistral-Small-3.  Phi-4 is fallback only.
    assert profiles["default"] == "mistral", (
        "v18 charter pins Mistral-Small-3 as the primary judge"
    )


# ─── Per-profile invariants ──────────────────────────────────────


REQUIRED_FIELDS = {
    "label",
    "model_name",
    "gguf_filename",
    "huggingface_repo",
    "huggingface_pattern",
    "approx_disk_gb",
    "container_memory",
    "container_cpus",
    "ctx_size",
    "threads",
    "request_timeout_s",
    "max_tokens",
    "rationale",
}


def test_every_profile_has_required_fields(profiles):
    for name, prof in profiles["profiles"].items():
        missing = REQUIRED_FIELDS - set(prof.keys())
        assert not missing, f"profile {name!r}: missing fields {missing}"


def test_gguf_filename_ends_with_gguf(profiles):
    for name, prof in profiles["profiles"].items():
        assert prof["gguf_filename"].endswith(".gguf"), (
            f"profile {name!r}: gguf_filename must end in .gguf, "
            f"got {prof['gguf_filename']!r}"
        )


def test_disk_size_is_positive(profiles):
    for name, prof in profiles["profiles"].items():
        assert prof["approx_disk_gb"] > 0, (
            f"profile {name!r}: approx_disk_gb must be > 0"
        )
        # Sanity: nothing should claim < 5 GB at Q3 or > 30 GB on
        # this hardware budget.
        assert 5 < prof["approx_disk_gb"] < 30, (
            f"profile {name!r}: approx_disk_gb out of plausible range "
            f"({prof['approx_disk_gb']})"
        )


def test_timeouts_are_reasonable(profiles):
    for name, prof in profiles["profiles"].items():
        assert 60 <= prof["request_timeout_s"] <= 1200, (
            f"profile {name!r}: request_timeout_s={prof['request_timeout_s']} "
            "outside plausible CPU-judge range (60-1200s)"
        )


def test_ctx_size_supports_judge_prompt(profiles):
    # Judge prompts run ~1500 tokens (system + 2 candidates + rubrics);
    # 4096 ctx is the comfortable default.
    for name, prof in profiles["profiles"].items():
        assert prof["ctx_size"] >= 2048, (
            f"profile {name!r}: ctx_size {prof['ctx_size']} too small "
            "for the position-swap + 2-rubric prompt"
        )


def test_max_tokens_capped_at_reasonable(profiles):
    for name, prof in profiles["profiles"].items():
        assert 64 <= prof["max_tokens"] <= 512, (
            f"profile {name!r}: max_tokens out of range "
            f"({prof['max_tokens']})"
        )


# ─── Mistral-vs-Phi4 separation ──────────────────────────────────


def test_mistral_and_phi4_both_present(profiles):
    # The brief explicitly requires Phi-4 as a documented fallback.
    assert "mistral" in profiles["profiles"]
    assert "phi4" in profiles["profiles"]


def test_mistral_uses_a_qwen_unrelated_family(profiles):
    """v18 charter requires the judge to be FAMILY-DISTINCT from the
    Qwen-derived AMOR architect.  Mistral is the canonical pick."""
    mistral = profiles["profiles"]["mistral"]
    label_l = mistral["label"].lower()
    assert "mistral" in label_l, (
        "the 'mistral' profile must actually be a Mistral model "
        "(Panickssery 2024 self-correlation guard)"
    )


def test_phi4_lighter_than_mistral(profiles):
    # Phi-4 fallback's purpose is to fit in less RAM.
    mistral_disk = profiles["profiles"]["mistral"]["approx_disk_gb"]
    phi4_disk = profiles["profiles"]["phi4"]["approx_disk_gb"]
    assert phi4_disk < mistral_disk, (
        "phi4 must be lighter than mistral; otherwise it isn't a real "
        "fallback"
    )
