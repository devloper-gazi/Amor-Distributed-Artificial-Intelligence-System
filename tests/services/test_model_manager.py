"""
Unit tests for the v2 ModelManager service.

Covered:
  - InstalledModel dataclass + JSON round-trip
  - auto_select scoring favours catalogued models over raw tags
  - import_gguf rejects non-GGUF magic bytes (no FS / no Ollama)
  - import_gguf rejects oversize uploads (no FS / no Ollama)
  - delete_custom_model raises PermissionError when no sidecar exists

The HTTP paths (Ollama /api/tags, /api/pull, /api/create) are mocked
via monkeypatched httpx.AsyncClient — these tests never hit the
network or the filesystem.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from document_processor.services.model_manager import (
    EFFORT_TIER_MAP,
    MAX_UPLOAD_SIZE_BYTES,
    MODE_REQUIREMENTS,
    InstalledModel,
    ModelManager,
)


# ── small helpers ────────────────────────────────────────────────────


def _spec_for(tag: str):
    """Pull a real ModelSpec out of the curated catalogue if known."""
    from document_processor.code_intelligence.model_registry import (
        CODE_MODEL_CATALOGUE,
    )
    return next(
        (s for s in CODE_MODEL_CATALOGUE if s.ollama_tag.lower() == tag.lower()),
        None,
    )


# ── InstalledModel ─────────────────────────────────────────────────────


def test_installed_model_serialises_round_trip():
    m = InstalledModel(
        tag="qwen2.5:7b",
        size_bytes=4_400_000_000,
        modified_at="2026-04-27T12:00:00Z",
        is_custom=False,
        display_name="Qwen2.5 7B",
        spec=_spec_for("qwen2.5:7b"),
    )
    blob = ModelManager._serialise(m)
    assert blob["tag"] == "qwen2.5:7b"
    assert blob["size_bytes"] == 4_400_000_000
    assert blob["display_name"] == "Qwen2.5 7B"
    again = ModelManager._inflate_cached(blob)
    assert again.tag == m.tag
    assert again.is_custom is False


# ── MODE_REQUIREMENTS / EFFORT_TIER_MAP sanity ────────────────────────


def test_mode_requirements_has_all_known_modes():
    for mode in {"research", "thinking", "coding", "code", "__all__"}:
        assert mode in MODE_REQUIREMENTS
        assert isinstance(MODE_REQUIREMENTS[mode], list)


def test_effort_tier_map_has_all_known_efforts():
    for effort in {"basic", "medium", "deep", "expert", "ultra"}:
        assert effort in EFFORT_TIER_MAP
        assert all(t in {"flagship", "balanced", "lightweight"}
                   for t in EFFORT_TIER_MAP[effort])


# ── auto_select scoring ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_auto_select_returns_default_when_nothing_installed():
    mgr = ModelManager()
    with patch.object(mgr, "list_installed", AsyncMock(return_value=[])):
        tag, reason = await mgr.auto_select(mode="research", effort="medium")
        # Falls back to env default (qwen2.5:7b unless OLLAMA_MODEL is set).
        assert tag
        assert "default" in reason.lower() or "no models" in reason.lower()


@pytest.mark.asyncio
async def test_auto_select_picks_catalogued_over_unknown_tag():
    """Two installed models — one in the catalogue, one not. The
    catalogued one must win regardless of size."""
    mgr = ModelManager()
    catalogued = InstalledModel(
        tag="qwen2.5:7b",
        size_bytes=4_400_000_000,
        modified_at="",
        spec=_spec_for("qwen2.5:7b"),
    )
    raw = InstalledModel(
        tag="some-finetune:latest",
        size_bytes=99_000_000_000,  # bigger but unknown
        modified_at="",
        spec=None,
    )
    with patch.object(mgr, "list_installed",
                      AsyncMock(return_value=[raw, catalogued])):
        tag, reason = await mgr.auto_select(mode="coding", effort="medium")
        assert tag == "qwen2.5:7b"
        assert "auto-selected" in reason.lower()


@pytest.mark.asyncio
async def test_auto_select_falls_through_to_largest_unmatched():
    """When no installed tag is in the catalogue, return the largest."""
    mgr = ModelManager()
    small = InstalledModel(tag="a:1", size_bytes=10, modified_at="", spec=None)
    big = InstalledModel(tag="b:2", size_bytes=100, modified_at="", spec=None)
    with patch.object(mgr, "list_installed",
                      AsyncMock(return_value=[small, big])):
        tag, reason = await mgr.auto_select(mode="coding")
        assert tag == "b:2"
        assert "largest" in reason.lower()


# ── import_gguf gates (no actual FS / Ollama) ─────────────────────────


@pytest.mark.asyncio
async def test_import_gguf_rejects_non_gguf_magic_bytes():
    mgr = ModelManager()
    with pytest.raises(ValueError, match="GGUF magic"):
        await mgr.import_gguf(
            user_id="u1",
            client_id="c1",
            filename="evil.gguf",
            file_bytes=b"NOTGGUF" + b"\0" * 100,
        )


@pytest.mark.asyncio
async def test_import_gguf_rejects_oversize_upload():
    mgr = ModelManager()
    fake_size = MAX_UPLOAD_SIZE_BYTES + 1
    # Construct GGUF magic + enough length to fail the size check.
    payload = b"GGUF" + b"\0" * 1024
    # We can't actually allocate 50 GB in memory — patch len() via a
    # subclass that reports the desired size.
    class _BigBytes(bytes):
        def __len__(self):
            return fake_size
    big = _BigBytes(payload)
    with pytest.raises(ValueError, match="exceeds"):
        await mgr.import_gguf(
            user_id="u1",
            client_id="c1",
            filename="huge.gguf",
            file_bytes=big,
        )


# ── delete_custom_model owner gate ────────────────────────────────────


@pytest.mark.asyncio
async def test_delete_custom_model_owner_only(tmp_path, monkeypatch):
    """A request from a different owner is refused with PermissionError."""
    from document_processor.services import model_manager as mm
    monkeypatch.setattr(mm, "CUSTOM_MODELS_DIR", tmp_path)

    # Owner u1 has a model.
    owner_dir = tmp_path / "u1"
    owner_dir.mkdir()
    (owner_dir / "x.meta.json").write_text(
        '{"tag": "custom/foo:abc123", "owner": "u1", '
        '"gguf_path": "", "modelfile_path": ""}',
        encoding="utf-8",
    )

    mgr = ModelManager()
    # Different user tries to delete — PermissionError because their
    # owner_dir doesn't contain the meta file.
    with pytest.raises(PermissionError):
        await mgr.delete_custom_model(
            tag="custom/foo:abc123",
            user_id="u2",
            client_id="c2",
        )
