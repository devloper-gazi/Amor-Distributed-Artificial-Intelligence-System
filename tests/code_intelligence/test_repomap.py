"""Tests for RepoMap — workspace symbol graph + token-budgeted summary."""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from document_processor.code_intelligence.repomap import (
    RepoMap,
    _estimate_tokens,
    _regex_python_snapshot,
)


@pytest.fixture
def tiny_workspace(tmp_path: Path) -> Path:
    # Build a minimal workspace mimicking the AMOR layout.
    (tmp_path / "document_processor").mkdir()
    (tmp_path / "document_processor" / "engine.py").write_text(textwrap.dedent("""
        import os
        from typing import Dict

        class Engine:
            def __init__(self): pass
            async def run(self) -> Dict[str, int]: return {}

        async def helper_fn(x: int) -> int:
            return x + 1
    """).strip(), encoding="utf-8")
    (tmp_path / "document_processor" / "store.py").write_text(textwrap.dedent("""
        from .engine import Engine

        class Store:
            def save(self, e: Engine): pass
    """).strip(), encoding="utf-8")
    (tmp_path / ".gitignore").write_text("__pycache__/\n*.pyc\n", encoding="utf-8")
    return tmp_path


def test_estimate_tokens_rough_ratio():
    assert _estimate_tokens("") == 1
    # 12 chars / 4 ~= 3 tokens
    assert _estimate_tokens("hello world!") == 3


def test_python_regex_snapshot_extracts_symbols():
    src = textwrap.dedent("""
        import json
        from typing import List

        class Foo:
            def bar(self):
                pass

        async def baz():
            return 1
    """).strip()
    snap = _regex_python_snapshot("foo.py", src)
    names = {s.name for s in snap.symbols}
    assert "Foo" in names
    assert "bar" in names
    assert "baz" in names
    assert any(i.startswith("typing") or i == "json" for i in snap.imports)


def test_repomap_builds_from_workspace(tiny_workspace):
    rm = RepoMap(tiny_workspace, scope=["document_processor"])
    n = rm.build()
    assert n == 2
    paths = set(rm.snapshots.keys())
    assert "document_processor/engine.py" in paths
    assert "document_processor/store.py" in paths


def test_repomap_ranks_focused_file_first(tiny_workspace):
    rm = RepoMap(tiny_workspace, scope=["document_processor"])
    rm.build()
    ranked = rm.rank(focus_files=["document_processor/store.py"], boost=100.0)
    assert ranked[0][0] == "document_processor/store.py"


def test_repomap_render_under_budget(tiny_workspace):
    rm = RepoMap(tiny_workspace, scope=["document_processor"])
    rm.build()
    out = rm.repo_map(budget_tokens=200)
    assert out.startswith("# RepoMap")
    assert "document_processor" in out
    assert _estimate_tokens(out) <= 250  # small slack


def test_repomap_skips_files_outside_scope(tiny_workspace):
    # Add a file in a directory we don't include.
    other = tiny_workspace / "scripts"
    other.mkdir()
    (other / "noise.py").write_text("x = 1\n", encoding="utf-8")
    rm = RepoMap(tiny_workspace, scope=["document_processor"])
    rm.build()
    assert "scripts/noise.py" not in rm.snapshots


def test_repomap_respects_max_files(tmp_path):
    big = tmp_path / "document_processor"
    big.mkdir()
    for i in range(10):
        (big / f"m{i}.py").write_text("x = 1\n", encoding="utf-8")
    rm = RepoMap(tmp_path, scope=["document_processor"], max_files=4)
    n = rm.build()
    assert n == 4


def test_repomap_handles_empty_workspace(tmp_path):
    rm = RepoMap(tmp_path, scope=["nonexistent"])
    assert rm.build() == 0
    out = rm.repo_map(budget_tokens=200)
    # Should still produce a valid header, even if empty.
    assert out.startswith("# RepoMap")
