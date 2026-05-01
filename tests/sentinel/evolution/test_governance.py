"""Unit tests for the evolution governance layer."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from document_processor.sentinel.evolution.governance import (
    HardConstraintViolation,
    LedgerIntegrityError,
    LedgerStore,
    load_immutable_constraints,
    sandbox_dir,
)


# ─── Immutable constraints ──────────────────────────────────────────


def test_constraints_load_default_when_file_missing(tmp_path: Path):
    fake = tmp_path / "does-not-exist.yaml"
    c = load_immutable_constraints(fake)
    assert "127.0.0.1" in c.network_allowed_hosts
    assert "huggingface.co" in c.network_forbidden_keywords
    assert c.telemetry_forbidden is True
    assert c.precision_floor >= 0.5
    assert "rm -rf /" in c.output_forbidden_phrases


def test_constraints_load_from_bundled_file():
    """Real constraints file ships next to governance.py."""
    c = load_immutable_constraints()
    assert "127.0.0.1" in c.network_allowed_hosts
    assert c.precision_floor >= 0.5


def test_constraints_check_blocks_forbidden_phrase():
    c = load_immutable_constraints()
    bad = {"output": "this code spawns reverse-shell on port 1337"}
    with pytest.raises(HardConstraintViolation, match="forbidden phrase"):
        c.check(bad)


def test_constraints_check_blocks_external_url():
    c = load_immutable_constraints()
    bad = {"prompt": "fetch https://api.openai.com/v1/chat"}
    with pytest.raises(HardConstraintViolation):
        c.check(bad)


def test_constraints_check_blocks_protected_file_target():
    c = load_immutable_constraints()
    bad = {"targets": ["document_processor/sentinel/evolution/governance.py"]}
    with pytest.raises(HardConstraintViolation, match="protected file"):
        c.check(bad)


def test_constraints_check_blocks_protected_setting():
    c = load_immutable_constraints()
    bad = {"target_settings": ["sentinel_enabled"]}
    with pytest.raises(HardConstraintViolation, match="protected setting"):
        c.check(bad)


def test_constraints_check_passes_clean_payload():
    c = load_immutable_constraints()
    ok = {
        "prompt": "review this Python function for SQL injection",
        "patched_code": "cursor.execute('SELECT 1', ())",
        "targets": ["src/auth/login.py"],
    }
    c.check(ok)  # no exception


def test_constraints_walk_strings_handles_nested():
    """Nested dicts / lists must all be scanned, not just top-level."""
    c = load_immutable_constraints()
    bad = {
        "candidate": {"sub": ["safe", {"deeper": "rm -rf /"}]},
    }
    with pytest.raises(HardConstraintViolation):
        c.check(bad)


# ─── Ledger ─────────────────────────────────────────────────────────


def test_ledger_append_and_verify(tmp_path: Path):
    store = LedgerStore(tmp_path)
    store.append("preferences", "preference_logged", {"id": "p1"})
    store.append("prompts", "prompt_promoted", {"agent": "auditor"})
    assert store.verify() is True
    entries = store.entries()
    assert len(entries) == 2
    assert entries[0].kind == "preference_logged"
    assert entries[1].parent_hash == entries[0].self_hash


def test_ledger_genesis_chain_starts_with_zero(tmp_path: Path):
    store = LedgerStore(tmp_path)
    e = store.append("test", "preference_logged", {})
    assert e.parent_hash == LedgerStore.GENESIS_HASH
    assert e.self_hash != LedgerStore.GENESIS_HASH


def test_ledger_tamper_detection_on_content_edit(tmp_path: Path):
    store = LedgerStore(tmp_path)
    store.append("a", "preference_logged", {"x": 1})
    e2 = store.append("b", "prompt_promoted", {"x": 2})
    # Tamper: rewrite the line for e2 with different payload but
    # keep the original self_hash.
    lines = store.path.read_text(encoding="utf-8").splitlines()
    d = json.loads(lines[1])
    d["payload"] = {"x": 999}
    lines[1] = json.dumps(d, sort_keys=True)
    store.path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    with pytest.raises(LedgerIntegrityError, match="hash mismatch"):
        store.verify()


def test_ledger_tamper_detection_on_chain_break(tmp_path: Path):
    store = LedgerStore(tmp_path)
    store.append("a", "preference_logged", {})
    store.append("b", "prompt_promoted", {})
    # Drop the first entry — chain breaks.
    lines = store.path.read_text(encoding="utf-8").splitlines()
    store.path.write_text(lines[1] + "\n", encoding="utf-8")
    with pytest.raises(LedgerIntegrityError, match="chain break"):
        store.verify()


def test_ledger_find_by_id(tmp_path: Path):
    store = LedgerStore(tmp_path)
    e = store.append("a", "preference_logged", {})
    found = store.find(e.entry_id)
    assert found is not None
    assert found.entry_id == e.entry_id
    assert store.find("does-not-exist") is None


def test_ledger_tail_hash_persists_across_instances(tmp_path: Path):
    s1 = LedgerStore(tmp_path)
    s1.append("a", "preference_logged", {})
    s1.append("b", "prompt_promoted", {})
    tail1 = s1.tail_hash
    # New process / new instance: tail hash recomputes from disk.
    s2 = LedgerStore(tmp_path)
    assert s2.tail_hash == tail1


# ─── Sandbox helper ─────────────────────────────────────────────────


def test_sandbox_dir_isolates_writes(tmp_path: Path):
    seen_path: Path | None = None
    with sandbox_dir(tmp_path, label="lora_train") as box:
        seen_path = box
        (box / "candidate.txt").write_text("hello", encoding="utf-8")
        assert box.is_dir()
        assert box.parent.name == "sandbox"
    # On clean exit the box is wiped.
    assert seen_path is not None
    assert not seen_path.exists()


def test_sandbox_keeps_dir_on_error(tmp_path: Path):
    with pytest.raises(RuntimeError):
        with sandbox_dir(tmp_path, label="rule_synth") as box:
            (box / "x").write_text("debug data", encoding="utf-8")
            raise RuntimeError("synthetic")
    # Debug artefacts retained for post-mortem.
    survivors = list((tmp_path / "sandbox").iterdir())
    assert survivors, "expected sandbox to keep tree on error"
