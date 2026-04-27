"""Tests for CapabilityDiscoverer + license/metadata gates + registry."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from document_processor.code_intelligence.capability_discoverer import (
    CapabilityCandidate,
    CapabilityDiscoverer,
    CapabilityKind,
    CapabilityRecord,
    CapabilityRegistry,
    license_gate,
    metadata_gate,
)


def _candidate(**kwargs) -> CapabilityCandidate:
    base = dict(
        kind=CapabilityKind.TOOL,
        name="example/repo",
        source="github",
        package_or_endpoint="https://github.com/example/repo",
        spdx_license="MIT",
        stars=120,
        last_commit_iso=datetime.now(UTC).isoformat(),
        description="A demo",
    )
    base.update(kwargs)
    return CapabilityCandidate(**base)


# ── License gate ────────────────────────────────────────────────────────────


def test_license_gate_passes_apache():
    res = license_gate(_candidate(spdx_license="Apache-2.0"))
    assert res.passed is True


def test_license_gate_passes_mit():
    res = license_gate(_candidate(spdx_license="MIT"))
    assert res.passed is True


def test_license_gate_rejects_agpl():
    res = license_gate(_candidate(spdx_license="AGPL-3.0"))
    assert res.passed is False
    assert "rejected" in res.detail.lower() or "agpl" in res.detail.lower()


def test_license_gate_accepts_agpl_with_override():
    res = license_gate(
        _candidate(name="acme/agpl-thing", spdx_license="AGPL-3.0"),
        overrides=["acme/agpl-thing"],
    )
    assert res.passed is True
    assert "human-flagged" in res.detail


def test_license_gate_rejects_missing():
    res = license_gate(_candidate(spdx_license=""))
    assert res.passed is False
    assert "no SPDX" in res.detail


def test_license_gate_rejects_proprietary():
    res = license_gate(_candidate(spdx_license="Proprietary"))
    assert res.passed is False


# ── Metadata gate ───────────────────────────────────────────────────────────


def test_metadata_gate_passes_high_stars_recent_commit():
    res = metadata_gate(_candidate(stars=500))
    assert res.passed is True


def test_metadata_gate_rejects_low_stars():
    res = metadata_gate(_candidate(stars=10), min_stars=50)
    assert res.passed is False
    assert "stars" in res.detail.lower()


def test_metadata_gate_rejects_old_commit():
    old = (datetime.now(UTC) - timedelta(days=900)).isoformat()
    res = metadata_gate(_candidate(last_commit_iso=old), max_commit_age_days=540)
    assert res.passed is False
    assert "last_commit" in res.detail


def test_metadata_gate_passes_when_commit_unparseable_and_skipped():
    # No iso → no age check possible → passes by virtue of star count
    res = metadata_gate(_candidate(last_commit_iso=""))
    assert res.passed is True


def test_metadata_gate_skips_star_check_for_non_github():
    res = metadata_gate(
        _candidate(source="huggingface", stars=0),
        min_stars=50,
    )
    assert res.passed is True


# ── Registry ────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_registry_in_process_fallback_register_and_list():
    reg = CapabilityRegistry()
    record = CapabilityRecord(
        id="abc-123",
        kind="tool",
        name="acme/example",
        package_or_endpoint="https://example",
        spdx_license="MIT",
        registered_at="2026-01-01T00:00:00+00:00",
    )
    await reg.register(record)
    out = await reg.list_all()
    names = {r["name"] for r in out}
    assert "acme/example" in names


@pytest.mark.asyncio
async def test_registry_unregister():
    reg = CapabilityRegistry()
    record = CapabilityRecord(
        id="def-456",
        kind="tool",
        name="acme/another",
        package_or_endpoint="https://x",
        spdx_license="MIT",
        registered_at="2026-01-01T00:00:00+00:00",
    )
    await reg.register(record)
    assert await reg.unregister("acme/another") is True
    assert (await reg.get("acme/another")) is None


@pytest.mark.asyncio
async def test_registry_get_returns_record():
    reg = CapabilityRegistry()
    record = CapabilityRecord(
        id="ghi-789",
        kind="model",
        name="acme/embedder",
        package_or_endpoint="acme/embedder",
        spdx_license="Apache-2.0",
        registered_at="2026-01-01T00:00:00+00:00",
    )
    await reg.register(record)
    fetched = await reg.get("acme/embedder")
    assert fetched is not None
    assert fetched["spdx_license"] == "Apache-2.0"


# ── Discoverer single-cycle (mocked) ────────────────────────────────────────


@pytest.mark.asyncio
async def test_run_once_returns_report_even_with_no_sources(monkeypatch):
    """All sources unavailable → cycle still completes with empty result."""
    from document_processor.code_intelligence import capability_discoverer as cd

    async def _empty(*args, **kwargs):
        return []

    monkeypatch.setattr(cd, "_discover_hugging_face", _empty)
    monkeypatch.setattr(cd, "_discover_github", _empty)
    monkeypatch.setattr(cd, "_discover_arxiv", _empty)

    d = CapabilityDiscoverer(interval_s=3600, max_per_cycle=1)
    report = await d.run_once()
    assert report["cycle"] == 1
    assert report["candidates_seen"] == 0
    assert report["accepted"] == []


@pytest.mark.asyncio
async def test_run_once_registers_passing_candidate(monkeypatch):
    """Inject one well-formed candidate → it should pass gates and register."""
    from document_processor.code_intelligence import capability_discoverer as cd

    async def _one_hf(*args, **kwargs):
        return [
            _candidate(
                kind=CapabilityKind.MODEL,
                name="hf/test-model",
                source="huggingface",
                package_or_endpoint="hf/test-model",
                spdx_license="Apache-2.0",
            )
        ]

    async def _empty(*args, **kwargs):
        return []

    monkeypatch.setattr(cd, "_discover_hugging_face", _one_hf)
    monkeypatch.setattr(cd, "_discover_github", _empty)
    monkeypatch.setattr(cd, "_discover_arxiv", _empty)

    d = CapabilityDiscoverer(interval_s=3600, max_per_cycle=5)
    report = await d.run_once()
    assert report["candidates_seen"] == 1
    assert report["candidates_novel"] == 1
    assert len(report["accepted"]) == 1
    assert report["accepted"][0]["name"] == "hf/test-model"


@pytest.mark.asyncio
async def test_run_once_rejects_bad_license(monkeypatch):
    from document_processor.code_intelligence import capability_discoverer as cd

    async def _gpl(*args, **kwargs):
        return [
            _candidate(
                name="evil/agpl",
                source="github",
                spdx_license="AGPL-3.0",
                package_or_endpoint="https://github.com/evil/agpl",
            )
        ]

    async def _empty(*args, **kwargs):
        return []

    monkeypatch.setattr(cd, "_discover_hugging_face", _empty)
    monkeypatch.setattr(cd, "_discover_github", _gpl)
    monkeypatch.setattr(cd, "_discover_arxiv", _empty)

    d = CapabilityDiscoverer(interval_s=3600)
    report = await d.run_once()
    assert report["accepted"] == []
    assert any(r["stage"] == "license" for r in report["rejected"])


@pytest.mark.asyncio
async def test_strict_mode_marks_sandbox_install_unimplemented(monkeypatch):
    from document_processor.code_intelligence import capability_discoverer as cd

    monkeypatch.setenv("CODE_CAPABILITY_STRICT", "true")

    async def _ok(*args, **kwargs):
        return [
            _candidate(
                name="mit/cool-tool",
                source="github",
                spdx_license="MIT",
                stars=200,
            )
        ]

    async def _empty(*args, **kwargs):
        return []

    monkeypatch.setattr(cd, "_discover_hugging_face", _empty)
    monkeypatch.setattr(cd, "_discover_github", _ok)
    monkeypatch.setattr(cd, "_discover_arxiv", _empty)

    d = CapabilityDiscoverer(interval_s=3600)
    report = await d.run_once()
    assert report["accepted"] == []
    assert any(r["stage"] == "sandbox_install" for r in report["rejected"])
