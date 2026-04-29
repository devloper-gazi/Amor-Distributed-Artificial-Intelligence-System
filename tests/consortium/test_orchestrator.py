"""
Unit tests for ConsortiumOrchestrator — quality gates, prompt
composition, artifact bundling. The actual research/thinking/code
engines are mocked at the import boundary so the test runs in
milliseconds and never hits Ollama / Mongo / web search.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from document_processor.consortium import (
    ConsortiumBundle,
    ConsortiumOrchestrator,
    ConsortiumScope,
)
from document_processor.consortium.models import (
    ImplementationArtifact,
    ResearchArtifact,
    ThinkingArtifact,
    VerificationGate,
)
from document_processor.consortium.orchestrator import _DEPTH_TO_PHASE_EFFORTS


# ── Scope phase ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_phase_scope_fills_in_defaults_when_triage_offline(monkeypatch):
    """If run_triage raises (Ollama down), the orchestrator still
    populates title / summary / efforts deterministically."""
    from document_processor.consortium import orchestrator as orch_mod

    async def boom_triage(*a, **kw):
        raise RuntimeError("Ollama unreachable")

    # Patch where the orchestrator imports it (lazy import inside _phase_scope).
    monkeypatch.setattr(
        "document_processor.code_intelligence.agents.run_triage",
        boom_triage,
    )

    scope = ConsortiumScope(goal="Build me a tiny CSV diff tool")
    orch = ConsortiumOrchestrator(session_id="t1", scope=scope)
    result = await orch._phase_scope()
    assert result.title.lower().startswith("build")
    assert result.summary
    assert result.success_criteria  # default fallbacks present
    assert result.constraints
    assert result.research_query  # distilled


def test_distill_research_query_strips_imperative_verbs():
    f = ConsortiumOrchestrator._distill_research_query
    assert f("Build me a CSV diff tool") == "CSV diff tool"
    assert f("design a queue system") == "queue system"
    assert f("write a parser") == "parser"
    # Idempotent on plain queries.
    assert f("graph database concepts") == "graph database concepts"


def test_derive_title_truncates_and_capitalizes():
    f = ConsortiumOrchestrator._derive_title
    assert f("hello world") == "Hello world"
    long = "this is an extremely long project goal that goes on and on and on and on"
    title = f(long)
    assert len(title) <= 65
    assert title[0].isupper()
    # Always end on a clean cut + ellipsis.
    assert title.endswith("…")


def test_depth_to_phase_efforts_covers_every_tier():
    for tier in {"basic", "medium", "deep", "expert", "ultra"}:
        assert tier in _DEPTH_TO_PHASE_EFFORTS
        assert set(_DEPTH_TO_PHASE_EFFORTS[tier].keys()) == {
            "research", "thinking", "implementation",
        }


# ── Quality gates ────────────────────────────────────────────────────


def test_gate_research_flags_missing_citations():
    artifact = ResearchArtifact(
        query="x", depth="medium",
        summary_markdown="A long summary " * 80,
        sources=[],
        citation_count=0,
    )
    gate = ConsortiumOrchestrator._gate_research(artifact)
    assert gate.phase == "research"
    assert gate.status == "failed"
    assert any("citation" in f.lower() for f in gate.findings)
    assert any("source" in f.lower() for f in gate.findings)


def test_gate_research_passes_with_solid_output():
    artifact = ResearchArtifact(
        query="x", depth="medium",
        summary_markdown="Lorem ipsum [1] " * 200,
        sources=[{"url": f"https://e.com/{i}"} for i in range(8)],
        citation_count=12,
    )
    gate = ConsortiumOrchestrator._gate_research(artifact)
    assert gate.status == "passed"
    assert gate.score >= 70


def test_gate_thinking_flags_missing_decision():
    art = ThinkingArtifact(
        deliverable_markdown="some doc",
        alternatives=[],
        decision={},
    )
    gate = ConsortiumOrchestrator._gate_thinking(art)
    assert any("alternative" in f.lower() for f in gate.findings)
    assert any("decision" in f.lower() for f in gate.findings)


def test_gate_implementation_penalises_critical_findings():
    art = ImplementationArtifact(
        code="print('hi')",
        tests=None,
        static_analysis={"critical_count": 2, "high_count": 1},
        execution_results=[],
    )
    gate = ConsortiumOrchestrator._gate_implementation(art)
    assert gate.status in {"failed", "passed_warn"}
    assert any("critical" in f.lower() for f in gate.findings)


def test_gate_implementation_rewards_passing_runs():
    art = ImplementationArtifact(
        code="print('hi')",
        tests="def test(): assert 1",
        static_analysis={"critical_count": 0, "high_count": 0},
        execution_results=[{"success": True}, {"success": True}],
        review={"verdict": "approve"},
    )
    gate = ConsortiumOrchestrator._gate_implementation(art)
    assert gate.status == "passed"


# ── Prompt composition ──────────────────────────────────────────────


def test_compose_thinking_prompt_includes_research():
    scope = ConsortiumScope(
        goal="Build a CSV diff tool",
        title="CSV diff",
        summary="A small CLI",
        constraints=["No external services"],
        success_criteria=["Tests pass"],
    )
    orch = ConsortiumOrchestrator(session_id="t", scope=scope)
    orch.research = ResearchArtifact(
        query="csv diff", depth="medium",
        summary_markdown="### Findings\n- pandas works\n- difflib too",
    )
    prompt = orch._compose_thinking_prompt()
    assert "CSV diff tool" in prompt
    assert "Constraints" in prompt
    assert "Success criteria" in prompt
    assert "Research summary" in prompt
    assert "pandas works" in prompt


def test_compose_implementation_context_carries_design():
    scope = ConsortiumScope(
        goal="x", constraints=["c1"], success_criteria=["s1"],
    )
    orch = ConsortiumOrchestrator(session_id="t", scope=scope)
    orch.thinking = ThinkingArtifact(
        deliverable_markdown="## Design\nUse difflib + argparse.",
    )
    orch.research = ResearchArtifact(
        query="x", depth="medium",
        summary_markdown="background",
    )
    ctx = orch._compose_implementation_context()
    assert "Constraints" in ctx
    assert "Success criteria" in ctx
    assert "Design document" in ctx
    assert "difflib" in ctx
    assert "Research summary" in ctx


# ── Bundle + artifact dir ───────────────────────────────────────────


@pytest.mark.asyncio
async def test_bundle_writes_artifact_directory(tmp_path: Path):
    scope = ConsortiumScope(
        goal="Build a thing", title="Thing", summary="A thing",
        language="python",
    )
    orch = ConsortiumOrchestrator(
        session_id="abc-1", scope=scope, artifact_dir=tmp_path / "out",
    )
    orch.research = ResearchArtifact(
        query="thing", depth="medium",
        summary_markdown="research",
        sources=[{"url": "https://e.com/1"}],
        citation_count=1,
    )
    orch.thinking = ThinkingArtifact(
        deliverable_markdown="# Design", alternatives=[{"a": 1}],
        decision={"chosen": "a"},
    )
    orch.implementation = ImplementationArtifact(
        code="import pygame\nprint('hi')",
        tests="import pytest\ndef test_hi(): assert 1",
        language="python", deliverable_markdown="## Implementation",
        static_analysis={"critical_count": 0},
        execution_results=[{"success": True}],
        review={
            "verdict": "approve",
            "summary": "Looks fine",
            "strengths": ["clean", "tested"],
            "weaknesses": [{"title": "no docstring", "detail": "main.py lacks a module docstring"}],
        },
    )
    orch.verifications = [
        VerificationGate(phase="research",       status="passed", score=80, summary="ok"),
        VerificationGate(phase="thinking",       status="passed", score=85, summary="ok"),
        VerificationGate(phase="implementation", status="passed", score=90, summary="ok"),
    ]
    bundle = await orch._bundle()
    assert isinstance(bundle, ConsortiumBundle)
    assert bundle.readme_markdown.startswith("# ")
    out = tmp_path / "out"

    # Top-level metadata stays at the root for stable contract.
    assert (out / "README.md").exists()
    assert (out / "scope.json").exists()
    assert (out / "verifications.json").exists()
    assert (out / "bundle.json").exists()

    # New conventional layout: src/, tests/, docs/, reports/.
    assert (out / "src" / "main.py").read_text(encoding="utf-8") == "import pygame\nprint('hi')"
    assert (out / "tests" / "test_main.py").read_text(encoding="utf-8") == "import pytest\ndef test_hi(): assert 1"
    assert (out / "tests" / "__init__.py").exists()
    assert (out / "docs" / "design.md").exists()
    assert (out / "docs" / "alternatives.json").exists()
    assert (out / "docs" / "research_summary.md").exists()
    assert (out / "docs" / "research_sources.json").exists()
    assert (out / "docs" / "review.md").exists()
    assert (out / "reports" / "static_analysis.json").exists()
    assert (out / "reports" / "execution_results.json").exists()

    # Python-specific scaffolding: pip detects only third-party imports
    # (pytest is included for the tests file; pygame for main).
    reqs = (out / "requirements.txt").read_text(encoding="utf-8").splitlines()
    assert "pygame" in reqs
    assert "pytest" in reqs
    # Stdlib must never leak into requirements.txt.
    assert all(x not in reqs for x in ("os", "sys", "json", "ast"))

    run_sh = (out / "run.sh").read_text(encoding="utf-8")
    assert run_sh.startswith("#!/usr/bin/env bash")
    # Run script invokes pytest via the venv's python — exact string is
    # `"$VENV/bin/python" -m pytest tests/`. Match the suffix so the test
    # is robust to variable-name churn.
    assert "-m pytest tests/" in run_sh
    assert "setup" in run_sh and "clean" in run_sh

    pyproject = (out / "pyproject.toml").read_text(encoding="utf-8")
    assert "[project]" in pyproject
    assert "pygame" in pyproject  # dep block carries detected pip names

    # The review is rendered as proper markdown, not a JSON dump.
    review_md = (out / "docs" / "review.md").read_text(encoding="utf-8")
    assert review_md.startswith("# Adversarial review")
    assert "approve" in review_md
    assert "no docstring" in review_md

    # README surfaces the Quick Start block when there's a Python entry point.
    readme = (out / "README.md").read_text(encoding="utf-8")
    assert "## Quick start" in readme
    assert "bash run.sh" in readme


# ── Artifact-bundle helpers (direct unit coverage) ──────────────────


def test_extract_python_requirements_filters_stdlib_and_aliases():
    """AST walk must catch top-level imports + map known aliases."""
    src_main = (
        "import os\n"
        "import sys\n"
        "import json\n"
        "import pygame\n"
        "from PIL import Image\n"
        "from bs4 import BeautifulSoup\n"
        "import cv2\n"
        "import yaml\n"
        "from sklearn.model_selection import train_test_split\n"
        "from . import helpers\n"  # relative — must be ignored
    )
    src_tests = "import pytest\nimport os\n"
    reqs = ConsortiumOrchestrator._extract_python_requirements(src_main, src_tests)
    # Stdlib filtered.
    for stdlib in ("os", "sys", "json"):
        assert stdlib not in reqs
    # Third-party aliases mapped to pip names.
    assert "Pillow" in reqs
    assert "beautifulsoup4" in reqs
    assert "opencv-python" in reqs
    assert "PyYAML" in reqs
    assert "scikit-learn" in reqs
    # Plain third-party preserved.
    assert "pygame" in reqs
    assert "pytest" in reqs
    # Result is sorted (stable ordering helps diffability).
    assert reqs == sorted(reqs)


def test_extract_python_requirements_handles_syntax_error():
    """Partially invalid code shouldn't break the bundle."""
    bad = "def broken(:\n    return\n"
    good = "import requests\n"
    reqs = ConsortiumOrchestrator._extract_python_requirements(bad, good)
    assert reqs == ["requests"]


def test_extract_python_requirements_empty_inputs():
    assert ConsortiumOrchestrator._extract_python_requirements() == []
    assert ConsortiumOrchestrator._extract_python_requirements(None, "") == []


def test_render_review_markdown_full_payload():
    md = ConsortiumOrchestrator._render_review_markdown({
        "verdict": "approve_with_changes",
        "score": 78,
        "severity": "medium",
        "summary": "Mostly good but missing edge cases.",
        "strengths": ["clear structure", "good naming"],
        "weaknesses": [
            {"title": "no input validation",
             "detail": "main.py does not check argv length"},
        ],
        "suggestions": ["Add type hints"],
        "untracked_field": "should land in raw payload",
    })
    assert md.startswith("# Adversarial review")
    assert "approve_with_changes" in md
    assert "Score**: 78" in md
    assert "Severity**: medium" in md
    assert "## Summary" in md
    assert "Mostly good" in md
    assert "## Strengths" in md
    assert "clear structure" in md
    assert "## Weaknesses" in md
    assert "no input validation" in md
    assert "## Suggestions" in md
    assert "Add type hints" in md
    assert "## Raw review payload" in md
    assert "untracked_field" in md


def test_render_review_markdown_empty_returns_placeholder():
    md = ConsortiumOrchestrator._render_review_markdown({})
    assert "No review produced" in md


def test_build_run_sh_includes_pytest_when_tests_present():
    sh = ConsortiumOrchestrator._build_run_sh(has_tests=True)
    assert sh.startswith("#!/usr/bin/env bash")
    assert "-m pytest tests/" in sh
    assert "case " in sh and "setup)" in sh and "clean)" in sh


def test_build_run_sh_skips_pytest_when_no_tests():
    sh = ConsortiumOrchestrator._build_run_sh(has_tests=False)
    assert "no tests in this bundle" in sh
    assert "pytest" not in sh


def test_build_pyproject_toml_quotes_summary_safely():
    toml = ConsortiumOrchestrator._build_pyproject_toml(
        name="My Cool Project!",
        summary='A "snake game" — fast & fun',
        requirements=["pygame", "Pillow"],
    )
    assert "[project]" in toml
    # Slug normalised: lowercase + non-alnum collapsed.
    assert 'name = "my-cool-project"' in toml
    # Quotes inside the summary are escaped, not raw.
    assert '\\"snake game\\"' in toml
    assert "pygame" in toml and "Pillow" in toml


def test_build_pyproject_toml_handles_empty_requirements():
    toml = ConsortiumOrchestrator._build_pyproject_toml(
        name="x", summary="", requirements=[],
    )
    assert "dependencies = []" in toml


# ── Pluggable implementation engine (quick_code dispatch) ───────────


@pytest.mark.asyncio
async def test_implementation_engine_quick_code_dispatches_to_quickcode_engine(
    monkeypatch,
):
    """When ConsortiumScope.implementation_engine == 'quick_code', the
    orchestrator must construct a QuickCodeEngine instead of a
    CodeIntelligenceEngine, then ingest its bundle through the
    to_implementation_artifact() adapter so downstream gate scoring
    + bundle writing don't change."""
    from document_processor.consortium import orchestrator as orch_mod
    from document_processor.consortium.models import ImplementationArtifact

    # Spy: a fake QuickCodeEngine that records its construction args
    # and returns a populated bundle synchronously.
    constructed: dict = {}

    class _SpyQuickCode:
        def __init__(self, *, session_id, request, on_event=None, **kw):
            constructed["session_id"] = session_id
            constructed["request"] = request
            constructed["on_event"] = on_event
            self._on_event = on_event

        async def run(self):
            from document_processor.quick_code.models import (
                QuickCodeAlternative, QuickCodeBundle,
                QuickCodeReasoning, QuickCodeRequest,
                QuickCodeVerification,
            )
            # Emit one inner event so we can verify the relay works.
            if self._on_event:
                await self._on_event({
                    "type": "quick_code_phase_complete", "phase": "verify",
                })
            return QuickCodeBundle(
                session_id="qc-sid",
                request=QuickCodeRequest(prompt="x", language="python"),
                code="print('quick')",
                tests="def test_q(): assert 1",
                reasoning=QuickCodeReasoning(
                    alternatives=[QuickCodeAlternative(
                        label="A",
                        scores={"clarity": 0.8, "math_soundness": 0.8,
                                "performance": 0.7, "edge_cases": 0.7},
                    )],
                    chosen_label="A",
                ),
                verification=QuickCodeVerification(score=85.0),
                deliverable_markdown="# Quick result",
            )

    # Patch the lazy `from ..quick_code import QuickCodeEngine` inside
    # _phase_implementation by monkey-patching the package attribute.
    # Python resolves `from pkg import Name` against `pkg.Name`, so
    # patching the package module catches the lazy import.
    import document_processor.quick_code as qc_module
    monkeypatch.setattr(qc_module, "QuickCodeEngine", _SpyQuickCode)

    relay_events: list[dict] = []

    async def collect(evt):
        relay_events.append(evt)

    scope = ConsortiumScope(
        goal="Build a thing", language="python",
        implementation_engine="quick_code",
    )
    orch = ConsortiumOrchestrator(
        session_id="cons-1", scope=scope, on_event=collect,
    )
    impl = await orch._phase_implementation()

    # Spy was constructed with the expected scope fields.
    assert constructed.get("session_id") == "cons-1"
    qc_req = constructed["request"]
    assert qc_req.prompt == "Build a thing"
    assert qc_req.language == "python"

    # Adapter mapped QuickCodeBundle → ImplementationArtifact correctly.
    assert isinstance(impl, ImplementationArtifact)
    assert impl.code == "print('quick')"
    assert impl.tests == "def test_q(): assert 1"
    assert impl.plan["engine"] == "quick_code"

    # Phase events made it out: phase_start, the inner relay event
    # (prefixed with consortium:implementation:), phase_complete with
    # engine="quick_code", and the consortium_gate.
    types = [e["type"] for e in relay_events]
    assert "consortium_phase_start" in types
    assert any(t.startswith("consortium:implementation:")
               for t in types)
    complete = next(
        e for e in relay_events
        if e["type"] == "consortium_phase_complete"
    )
    assert complete["phase"] == "implementation"
    assert complete.get("engine") == "quick_code"

    # Gate appended + emitted.
    assert len(orch.verifications) == 1
    assert orch.verifications[0].phase == "implementation"
    assert any(e["type"] == "consortium_gate" for e in relay_events)


@pytest.mark.asyncio
async def test_implementation_engine_default_unchanged():
    """Default scope still routes through the existing 9-phase
    CodeIntelligenceEngine — no regression."""
    scope = ConsortiumScope(goal="Build a thing", language="python")
    # No engine override → existing default.
    assert scope.implementation_engine == "code_intelligence"


# ── Cancellation ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_cancel_skips_remaining_phases():
    scope = ConsortiumScope(goal="x", cancel_requested=True)
    orch = ConsortiumOrchestrator(session_id="c1", scope=scope)
    bundle = await orch.run()
    # Cancellation surfaces as a completed bundle with status=cancelled
    # and no implementation artifact ever filled in.
    assert bundle.session_id == "c1"
    assert bundle.implementation is None or not bundle.implementation.code


# ── Constructor validation ──────────────────────────────────────────


def test_orchestrator_requires_non_empty_goal():
    with pytest.raises(ValueError, match="goal"):
        ConsortiumOrchestrator(
            session_id="x",
            scope=ConsortiumScope(goal="   "),
        )


# ── Bundle to_dict round-trip ───────────────────────────────────────


def test_bundle_to_dict_is_json_serialisable():
    import json
    scope = ConsortiumScope(goal="x", title="T")
    bundle = ConsortiumBundle(
        session_id="s", scope=scope,
        readme_markdown="# T",
    )
    s = json.dumps(bundle.to_dict())
    assert "T" in s
    assert "session_id" in s
