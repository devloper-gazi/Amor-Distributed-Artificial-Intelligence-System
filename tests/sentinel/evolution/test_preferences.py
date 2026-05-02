"""Unit tests for the preference-logging subsystem."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from document_processor.sentinel.evolution.preferences import (
    PreferencePair,
    PreferenceStore,
    ast_shape_proxy,
    code_hash,
    file_hash,
)


def test_code_hash_deterministic():
    h1 = code_hash("x = 1\n")
    h2 = code_hash("x = 1\n")
    assert h1 == h2
    assert len(h1) == 32


def test_file_hash_distinct():
    a = file_hash("/tmp/foo.py")
    b = file_hash("/tmp/bar.py")
    assert a != b


def test_ast_shape_same_for_renamed_identifiers():
    a = "def foo(x):\n    return x + 1\n"
    b = "def bar(y):\n    return y + 1\n"
    assert ast_shape_proxy(a) == ast_shape_proxy(b)


def test_ast_shape_different_for_different_structure():
    a = "def foo():\n    return 1\n"
    b = "class Foo:\n    pass\n"
    assert ast_shape_proxy(a) != ast_shape_proxy(b)


def test_ast_shape_empty_safe():
    assert ast_shape_proxy("") == ""


# ─── PreferenceStore ────────────────────────────────────────────────


def test_record_appends_jsonl_and_db(tmp_path: Path):
    store = PreferenceStore(tmp_path)
    pair = store.record(
        scan_id="scan-1",
        agent_name="auditor",
        user_action="mark_true_positive",
        chosen='{"verdict":"true_positive"}',
        rejected='{"verdict":"false_positive"}',
        file="src/auth.py",
        line_range="45-48",
        cwe="CWE-89",
        snippet="cursor.execute('SELECT * FROM users WHERE id=' + uid)",
        language="python",
    )
    assert pair.record_id
    assert pair.code_hash
    assert pair.ast_shape
    # JSONL append
    text = (tmp_path / "preferences.jsonl").read_text(encoding="utf-8")
    assert "scan-1" in text
    # DB count
    assert store.count() == 1
    assert store.count(agent_name="auditor") == 1
    assert store.count(agent_name="reasoner") == 0


def test_log_raw_code_disabled_by_default(tmp_path: Path):
    store = PreferenceStore(tmp_path)
    store.record(
        scan_id="s1", agent_name="auditor",
        user_action="mark_true_positive",
        chosen="ok", snippet="SECRET = 'AKIA-FAKE'",
    )
    blob = (tmp_path / "preferences.jsonl").read_text(encoding="utf-8")
    assert "AKIA-FAKE" not in blob, "raw code must NOT leak by default"


def test_log_raw_code_optin(tmp_path: Path):
    store = PreferenceStore(tmp_path, log_raw_code=True)
    store.record(
        scan_id="s2", agent_name="auditor",
        user_action="mark_true_positive",
        chosen="ok", snippet="literal-test-token",
    )
    blob = (tmp_path / "preferences.jsonl").read_text(encoding="utf-8")
    assert "literal-test-token" in blob


def test_query_by_agent(tmp_path: Path):
    store = PreferenceStore(tmp_path)
    for i in range(3):
        store.record(
            scan_id=f"s{i}", agent_name="auditor",
            user_action="mark_true_positive", chosen="x")
    for _ in range(2):
        store.record(
            scan_id="s9", agent_name="reasoner",
            user_action="mark_false_positive", chosen="x")
    assert len(store.by_agent("auditor")) == 3
    assert len(store.by_agent("reasoner")) == 2
    assert len(store.by_agent("redteam")) == 0


def test_query_by_cwe(tmp_path: Path):
    store = PreferenceStore(tmp_path)
    store.record(scan_id="s1", agent_name="auditor",
                 user_action="mark_true_positive",
                 chosen="x", cwe="CWE-89")
    store.record(scan_id="s2", agent_name="auditor",
                 user_action="mark_true_positive",
                 chosen="x", cwe="CWE-79")
    store.record(scan_id="s3", agent_name="auditor",
                 user_action="mark_true_positive",
                 chosen="x", cwe="CWE-89")
    assert len(store.by_cwe("CWE-89")) == 2
    assert len(store.by_cwe("CWE-79")) == 1


def test_export_dpo_dataset(tmp_path: Path):
    store = PreferenceStore(tmp_path)
    for i in range(5):
        store.record(
            scan_id=f"s{i}", agent_name="auditor",
            user_action="mark_true_positive",
            chosen=f"chosen-{i}", rejected=f"rejected-{i}",
            cwe="CWE-89",
        )
    out_path = tmp_path / "dpo.jsonl"
    rows = store.export_dpo_dataset(agent_name="auditor", path=out_path)
    assert rows == 5
    text = out_path.read_text(encoding="utf-8")
    for i in range(5):
        assert f"chosen-{i}" in text
        assert f"rejected-{i}" in text


def test_export_skips_records_without_rejected(tmp_path: Path):
    store = PreferenceStore(tmp_path)
    # Only chosen — no rejected; should be skipped from DPO export.
    store.record(scan_id="s1", agent_name="auditor",
                 user_action="mark_true_positive", chosen="x")
    out = tmp_path / "dpo.jsonl"
    rows = store.export_dpo_dataset(agent_name="auditor", path=out)
    assert rows == 0


def test_persistence_across_instances(tmp_path: Path):
    s1 = PreferenceStore(tmp_path)
    s1.record(scan_id="s1", agent_name="auditor",
              user_action="mark_true_positive", chosen="x")
    # Fresh instance reads existing data.
    s2 = PreferenceStore(tmp_path)
    assert s2.count() == 1
