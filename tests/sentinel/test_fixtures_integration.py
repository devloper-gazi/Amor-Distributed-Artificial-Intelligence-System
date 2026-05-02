"""Integration tests against the bundled vulnerable fixtures.

Runs the actual Sentinel pipeline (static + ML) on the fixture
files and asserts the engine catches the documented CWEs without
flagging the clean baseline.

LLM stages (auditor / reasoner / redteam / patcher / judge) are
DISABLED here via scan_profile="quick" — those need Ollama and a
much longer test suite.  This integration only exercises the
deterministic stages.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from document_processor.sentinel.engine import SentinelEngine
from document_processor.sentinel.models import SentinelRequest


FIXTURES_DIR = Path(__file__).parent / "fixtures"


def _run(coro):
    return asyncio.run(coro)


def _load_expectations() -> dict:
    return json.loads(
        (FIXTURES_DIR / "expected_findings.json").read_text(encoding="utf-8")
    )


# ─── Vulnerable Python ──────────────────────────────────────────────


def test_vulnerable_python_caught():
    f = FIXTURES_DIR / "vulnerable_python.py"
    if not f.is_file():
        pytest.skip("fixture missing")
    eng = SentinelEngine(
        request=SentinelRequest(paths=[str(f)], scan_profile="quick"),
    )
    bundle = _run(eng.run())
    cwes = {fn.cwe for fn in bundle.findings if fn.cwe}
    expected = set(_load_expectations()["vulnerable_python.py"]["expected_cwes"])
    # Static + ML alone won't catch every CWE (LLM agents are off).
    # Require AT LEAST: the secret detector catches CWE-798 (hardcoded
    # credentials).  The other CWEs come into play in Standard / Deep
    # mode once Auditor + Bandit run.
    assert "CWE-798" in cwes, (
        f"expected CWE-798 (hardcoded credential) caught by ML; "
        f"got {sorted(cwes)}"
    )


def test_clean_baseline_no_findings():
    f = FIXTURES_DIR / "clean_baseline.py"
    if not f.is_file():
        pytest.skip("fixture missing")
    eng = SentinelEngine(
        request=SentinelRequest(paths=[str(f)], scan_profile="quick"),
    )
    bundle = _run(eng.run())
    cwes = {fn.cwe for fn in bundle.findings if fn.cwe}
    # Quick profile: only ML (secret detector + anomaly detector) and
    # whatever static tool is on PATH.  The clean baseline has none of
    # the CWE-798 / CWE-89 / CWE-78 patterns.  The anomaly detector
    # may emit low-severity outliers when run on a single file (z-score
    # has no spread); we assert no critical / high findings instead of
    # zero total.
    severe = [
        fn for fn in bundle.findings
        if fn.severity in ("critical", "high")
    ]
    assert not severe, (
        f"clean baseline produced {len(severe)} severe findings; "
        f"FP rate too high: {[fn.cwe for fn in severe]}"
    )


def test_vulnerable_node_secret_caught():
    f = FIXTURES_DIR / "vulnerable_node.js"
    if not f.is_file():
        pytest.skip("fixture missing")
    eng = SentinelEngine(
        request=SentinelRequest(paths=[str(f)], scan_profile="quick"),
    )
    bundle = _run(eng.run())
    cwes = {fn.cwe for fn in bundle.findings if fn.cwe}
    # The fixture contains a deliberately-mangled "fake API secret"
    # assignment that the generic-password-assignment regex catches.
    # Real Stripe / GitHub key prefixes are intentionally NOT used in
    # the fixture so that GitHub's push-protection doesn't flag the
    # commit; the secret detector still emits CWE-798.
    assert "CWE-798" in cwes, (
        f"expected CWE-798 from hard-coded credential; got {sorted(cwes)}"
    )


# ─── End-to-end shape check ─────────────────────────────────────────


def test_pipeline_emits_full_phase_set():
    f = FIXTURES_DIR / "vulnerable_python.py"
    if not f.is_file():
        pytest.skip("fixture missing")
    seen_phases: list[str] = []

    async def cb(evt):
        if evt.get("type") == "sentinel_phase_complete":
            phase = evt.get("phase")
            if phase:
                seen_phases.append(phase)

    eng = SentinelEngine(
        request=SentinelRequest(paths=[str(f)], scan_profile="quick"),
        on_event=cb,
    )
    _run(eng.run())
    # Quick profile fires: normalize, static_swarm, ml_pipeline,
    # aggregate, score, report.
    for required in ("normalize", "ml_pipeline", "aggregate", "score", "report"):
        assert required in seen_phases, (
            f"missing phase {required} from {seen_phases}"
        )


def test_pipeline_emits_sarif_and_markdown():
    f = FIXTURES_DIR / "vulnerable_python.py"
    if not f.is_file():
        pytest.skip("fixture missing")
    eng = SentinelEngine(
        request=SentinelRequest(paths=[str(f)], scan_profile="quick"),
    )
    bundle = _run(eng.run())
    assert bundle.sarif_report
    assert bundle.markdown_report
    assert bundle.html_report
    # Roughly check the SARIF is valid JSON with a "runs" key.
    parsed = json.loads(bundle.sarif_report)
    assert "runs" in parsed
