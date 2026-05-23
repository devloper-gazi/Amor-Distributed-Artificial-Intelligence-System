"""
Tests for the plan-to-spec extractor (Cycle D Fix #5).

Drives the user-reported plan↔implementation gap: planner emitted
abstract steps ("use Doxygen / Sphinx") that the coder couldn't
ground in concrete signatures.  ``_extract_focused_spec`` compresses
the plan's spec block into a focused dict + (for C++) suggests STL
headers based on signature/dependency text.
"""

from __future__ import annotations

import pytest

from document_processor.code_intelligence.engine import (
    CodeIntelligenceEngine,
)


extract = CodeIntelligenceEngine._extract_focused_spec


def test_passthrough_when_no_spec_block():
    plan = {"language": "python", "steps": ["a", "b"]}
    out = extract(plan, "python")
    # No focused_spec added when nothing to add
    assert out is plan or "focused_spec" not in out


def test_extracts_spec_fields_from_plan():
    plan = {
        "language": "python",
        "spec": {
            "signatures": ["def fizzbuzz(n: int) -> list[str]"],
            "invariants": ["output length equals n"],
            "dependencies": ["pytest"],
            "files": ["fizzbuzz.py"],
        },
    }
    out = extract(plan, "python")
    spec = out["focused_spec"]
    assert "signatures" in spec
    assert "invariants" in spec
    assert "dependencies" in spec
    assert "files" in spec
    assert spec["signatures"] == ["def fizzbuzz(n: int) -> list[str]"]


def test_cpp_suggests_stl_headers_from_signatures():
    plan = {
        "language": "cpp",
        "spec": {
            "signatures": [
                "std::unordered_map<std::string, std::function<void()>> getHandlers()",
                "std::vector<int> compute(std::shared_ptr<Config> cfg)",
            ],
            "dependencies": [],
        },
    }
    out = extract(plan, "cpp")
    spec = out["focused_spec"]
    assert "suggested_includes" in spec
    headers = spec["suggested_includes"]
    assert "<unordered_map>" in headers
    assert "<string>" in headers
    assert "<functional>" in headers
    assert "<vector>" in headers
    assert "<memory>" in headers


def test_non_cpp_skips_suggested_includes():
    plan = {
        "language": "python",
        "spec": {"signatures": ["def f(x: list) -> dict"]},
    }
    out = extract(plan, "python")
    spec = out["focused_spec"]
    # Python has no suggested_includes mechanism
    assert "suggested_includes" not in spec


def test_does_not_mutate_original_plan():
    plan = {
        "language": "cpp",
        "spec": {"signatures": ["std::vector<int> f()"]},
    }
    out = extract(plan, "cpp")
    # Original is untouched
    assert "focused_spec" not in plan
    # New dict has the focused_spec
    assert "focused_spec" in out


def test_handles_dict_with_no_spec():
    plan = {"language": "rust"}
    out = extract(plan, "rust")
    assert out == plan


def test_handles_non_dict_input():
    out = extract([], "python")
    assert out == []
    out2 = extract(None, "python")
    assert out2 is None
