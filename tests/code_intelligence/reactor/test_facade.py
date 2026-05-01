"""
Tests for CodeSynthesisReactor facade — lazy subsystem construction,
verify_implementation envelope, fail-soft on disabled features.
"""

from __future__ import annotations

import json

import pytest

from document_processor.code_intelligence.reactor.config import ReactorConfig
from document_processor.code_intelligence.reactor.facade import (
    CodeSynthesisReactor,
    ReactorBundle,
)


class _FakeSandbox:
    """Returns scripted stdout per execute() call."""

    def __init__(self, stdout: str = "", skipped: bool = False):
        self.stdout = stdout
        self.skipped = skipped

    async def execute(self, code, language="python", timeout=30):
        stdout = self.stdout
        skipped = self.skipped

        class _R:
            def __init__(s):
                s.stdout = stdout
                s.stderr = ""
                s.exit_code = 0
                s.skipped = skipped
        return _R()


def _bench_payload(scale_ms):
    lines = ["BENCH_TARGET=f"]
    for scale, ms in scale_ms:
        lines.append(
            f'BENCH_RESULT={{"scale":{scale},"ms":{ms},"peak_kb":{scale * 10}}}'
        )
    return "\n".join(lines) + "\n"


def _property_payload(name_passed):
    lines = ["PROPERTY_TARGET=f"]
    for name, passed in name_passed:
        rec = {
            "name": name, "passed": passed,
            "samples_run": 50,
            "samples_failed": 0 if passed else 50,
            "first_failure_input": None if passed else "x",
            "first_failure_message": None if passed else "boom",
            "error": None,
        }
        lines.append("PROPERTY_RESULT=" + json.dumps(rec))
    return "\n".join(lines) + "\n"


# ── default config: every feature enabled ──────────────────────


def test_facade_with_default_config_has_all_subsystems_addressable():
    r = CodeSynthesisReactor(
        config=ReactorConfig.from_settings(),
        sandbox=_FakeSandbox(),
        llm_call=None,
    )
    assert r.config.enabled is True
    # Every feature addressable via the public API even when their
    # inputs aren't wired (subsystem accessor returns None gracefully).
    assert r._get_benchmarker() is not None  # sandbox provided
    assert r._get_property_runner() is not None
    # Tournament needs llm_call AND sandbox; missing llm → None.
    assert r._get_tournament() is None
    # RAG needs vector_store + embedder; both missing → None.
    assert r._get_rag() is None


def test_facade_with_disabled_master_gate_returns_none_for_subsystems():
    r = CodeSynthesisReactor(
        config=ReactorConfig(enabled=False),
        sandbox=_FakeSandbox(),
        llm_call=lambda *a, **kw: None,
    )
    assert r._get_benchmarker() is None
    assert r._get_property_runner() is None
    assert r._get_tournament() is None
    assert r._get_rag() is None


# ── verify_implementation: empty code ─────────────────────────


@pytest.mark.asyncio
async def test_verify_empty_code_returns_finding():
    r = CodeSynthesisReactor(
        config=ReactorConfig(),
        sandbox=_FakeSandbox(),
    )
    rb = await r.verify_implementation(
        code="", user_prompt="x",
    )
    assert isinstance(rb, ReactorBundle)
    assert any("no code" in f.lower() for f in rb.findings)


# ── verify_implementation: full happy path ────────────────────


@pytest.mark.asyncio
async def test_verify_runs_symbolic_bench_and_property_tests():
    """Sandbox is scripted to return bench AND property results in
    one call (the harness for each is independent — but the FakeSandbox
    just returns the same stdout regardless of script content)."""
    stdout = (
        _bench_payload([(10, 1.0), (100, 10.0), (1000, 100.0)])
        + _property_payload([("default_callable_no_exception", True)])
    )
    sandbox = _FakeSandbox(stdout=stdout)
    r = CodeSynthesisReactor(
        config=ReactorConfig(),
        sandbox=sandbox,
        llm_call=None,
    )
    rb = await r.verify_implementation(
        code="def f(xs): return sum(xs)\n",
        user_prompt="sum a list",
        triage={"task_type": "default", "language": "python"},
        claimed_complexity="O(n)",
    )
    # Symbolic ran (top-level def f).
    assert rb.symbolic is not None
    assert rb.symbolic["worst_bound"] == "O(1)"  # no loop in `sum`
    # Bench ran with linear growth; claim O(n) matches.
    assert rb.benchmark is not None
    assert rb.benchmark["fit"]["measured_label"] == "O(n)"
    assert rb.benchmark["claim_vs_measured"] == 0
    # Property tests ran (default catalogue invariant).
    assert rb.property_tests is not None
    assert rb.property_tests["all_passed"] is True


@pytest.mark.asyncio
async def test_verify_flags_underclaim_in_findings():
    """LLM claim O(n) but measured O(n^2) → finding."""
    stdout = (
        _bench_payload([(10, 1.0), (100, 100.0), (1000, 10_000.0)])
        + _property_payload([("default_callable_no_exception", True)])
    )
    sandbox = _FakeSandbox(stdout=stdout)
    r = CodeSynthesisReactor(
        config=ReactorConfig(),
        sandbox=sandbox,
    )
    rb = await r.verify_implementation(
        code="def f(xs): return xs\n",
        user_prompt="x", claimed_complexity="O(n)",
    )
    assert any("benchmark" in f.lower() and "claimed" in f.lower()
               for f in rb.findings)


# ── verify_implementation: feature subset ─────────────────────


@pytest.mark.asyncio
async def test_verify_skips_disabled_features():
    cfg = ReactorConfig(features={"symbolic_complexity"})
    r = CodeSynthesisReactor(
        config=cfg, sandbox=_FakeSandbox(),
    )
    rb = await r.verify_implementation(
        code="def f(): pass\n",
        user_prompt="x",
    )
    # Only symbolic should have populated.
    assert rb.symbolic is not None
    assert rb.benchmark is None
    assert rb.property_tests is None


@pytest.mark.asyncio
async def test_verify_handles_skipped_sandbox():
    """If the sandbox is unavailable (skipped=True), bench + property
    fail soft; symbolic still runs because it doesn't need the sandbox."""
    sandbox = _FakeSandbox(stdout="", skipped=True)
    r = CodeSynthesisReactor(
        config=ReactorConfig(), sandbox=sandbox,
    )
    rb = await r.verify_implementation(
        code="def f(): pass\n",
        user_prompt="x",
    )
    assert rb.symbolic is not None
    # Bench will have failed=True
    assert rb.benchmark is not None
    assert rb.benchmark["failed"] is True


# ── invariant generation ──────────────────────────────────────


@pytest.mark.asyncio
async def test_generate_invariants_returns_catalogue_only_when_no_llm():
    r = CodeSynthesisReactor(
        config=ReactorConfig(features={"property_tests"}),
        sandbox=_FakeSandbox(),
    )
    invs = await r.generate_invariants(
        triage={"task_type": "search"},
        user_prompt="binary search",
        llm_call=None,
    )
    assert any(i.source == "catalogue" for i in invs)
    assert not any(i.source == "llm" for i in invs)


@pytest.mark.asyncio
async def test_generate_invariants_includes_llm_suggestions_when_enabled():
    async def llm(prompt, system, max_tokens):
        return json.dumps({
            "invariants": [{
                "name": "llm_inv",
                "description": "x",
                "input_expr": "rng.randint(0, 1)",
                "assertion_expr": "True",
            }]
        })

    r = CodeSynthesisReactor(
        config=ReactorConfig(),
        sandbox=_FakeSandbox(),
        llm_call=llm,
    )
    invs = await r.generate_invariants(
        triage=None, user_prompt="x", llm_call=llm,
    )
    assert any(i.source == "llm" for i in invs)


# ── ReactorBundle to_dict shape ───────────────────────────────


def test_reactor_bundle_to_dict_has_expected_keys():
    rb = ReactorBundle(findings=["x"])
    d = rb.to_dict()
    for k in ("symbolic", "benchmark", "property_tests", "tournament",
              "rag_refs", "bandit_weights", "findings", "config"):
        assert k in d
    assert d["findings"] == ["x"]
