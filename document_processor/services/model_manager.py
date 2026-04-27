"""
ModelManager — unified LLM model management service.

Single source of truth for everything model-related:
  1. List installed Ollama models (cached, with catalogue enrichment)
  2. Auto-select the best installed model for mode + effort
  3. Resolve the effective model (user pref → wildcard → auto-select)
  4. Stream a pull from Ollama Hub
  5. Import a user-uploaded GGUF file via Modelfile + `ollama create`
  6. Delete a custom-uploaded model

Design notes
------------
* Reuses ``CodeModelRegistry``'s catalogue (12 curated tags) for
  enrichment so the selector UI shows benchmarks + tier + license
  for any tag that matches.
* Uses the existing ``chat_store`` per-user preference methods —
  no new collection class; that surface lives where every other
  user-scoped persistence already lives.
* Failure-quiet: Ollama unreachable → returns [] / falls back to
  ``OLLAMA_MODEL`` rather than 500-ing.
* GGUF upload writes to ``CUSTOM_MODELS_DIR/{owner}/`` and runs
  ``ollama create`` via subprocess — no third-party SDK.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, AsyncIterator

import httpx

from ..code_intelligence.model_registry import CODE_MODEL_CATALOGUE, ModelSpec
from ..infrastructure.cache import cache_manager

logger = logging.getLogger(__name__)


OLLAMA_BASE_URL = os.getenv("OLLAMA_BASE_URL", "http://ollama:11434")
OLLAMA_MODEL_DEFAULT = os.getenv("OLLAMA_MODEL", "qwen2.5:7b")
CUSTOM_MODELS_DIR = Path(os.getenv("CUSTOM_MODELS_DIR", "/models/custom"))
MAX_UPLOAD_SIZE_BYTES = (
    int(os.getenv("MAX_MODEL_UPLOAD_SIZE_GB", "50")) * 1024**3
)

# Mode → preferred strengths (used by auto-select scoring).
MODE_REQUIREMENTS: dict[str, list[str]] = {
    "research": ["general", "explanation", "planning", "fast inference"],
    "thinking": ["planning", "explanation", "agentic loops"],
    "coding": ["code generation", "debugging", "agentic loops"],
    "code": ["code generation", "debugging", "agentic loops"],
    "__all__": ["general"],
}

# Effort → ordered tier preference (mirrors CodeModelRegistry).
EFFORT_TIER_MAP: dict[str, list[str]] = {
    "basic": ["lightweight", "balanced", "flagship"],
    "medium": ["balanced", "lightweight", "flagship"],
    "deep": ["balanced", "flagship", "lightweight"],
    "expert": ["flagship", "balanced", "lightweight"],
    "ultra": ["flagship", "balanced", "lightweight"],
}

_PROBE_CACHE_KEY = "amor:model_manager:installed_models"
_PROBE_TTL = 120  # 2 minutes


@dataclass
class InstalledModel:
    """Enriched view of one installed Ollama model."""

    tag: str
    size_bytes: int
    modified_at: str
    is_custom: bool = False
    display_name: str | None = None
    spec: ModelSpec | None = None  # Match from CODE_MODEL_CATALOGUE if known


# ─────────────────────────────────────────────────────────────────────────────
# ModelManager
# ─────────────────────────────────────────────────────────────────────────────


class ModelManager:
    """Unified facade for installed-model listing, selection, pulling, and
    GGUF import. Singleton — one instance per app process, attached to
    ``app.state.model_manager`` in lifespan."""

    PREFERENCE_MODE_ALL = "__all__"

    def __init__(self) -> None:
        # The chat_store singleton owns the per-user preference methods.
        from ..infrastructure.chat_store import chat_store  # noqa: PLC0415
        self._store = chat_store

    # ── 1. LIST ───────────────────────────────────────────────────────────

    async def list_installed(
        self, force_refresh: bool = False,
    ) -> list[InstalledModel]:
        """Query Ollama /api/tags + enrich with catalogue. Cached 2 min."""
        if not force_refresh:
            try:
                cached = await cache_manager.get_json(_PROBE_CACHE_KEY)
                if isinstance(cached, list) and cached:
                    return [self._inflate_cached(m) for m in cached]
            except Exception:  # pragma: no cover
                pass

        try:
            async with httpx.AsyncClient(timeout=8.0) as client:
                resp = await client.get(f"{OLLAMA_BASE_URL}/api/tags")
                resp.raise_for_status()
                data = resp.json()
        except Exception as exc:
            logger.warning("model_manager_ollama_unreachable: %s", exc)
            return []

        catalogue_map: dict[str, ModelSpec] = {
            s.ollama_tag.lower(): s for s in CODE_MODEL_CATALOGUE
        }
        custom_tags: set[str] = self._scan_custom_tags()

        models: list[InstalledModel] = []
        for raw in data.get("models", []) or []:
            tag = str(raw.get("name") or "")
            if not tag:
                continue
            spec = catalogue_map.get(tag.lower())
            models.append(InstalledModel(
                tag=tag,
                size_bytes=int(raw.get("size") or 0),
                modified_at=str(raw.get("modified_at") or ""),
                is_custom=tag.lower() in custom_tags or tag.startswith("custom/"),
                display_name=(spec.display_name if spec else None),
                spec=spec,
            ))

        # Cache the JSON-friendly form (drops the ModelSpec dataclass).
        try:
            await cache_manager.set_json(
                _PROBE_CACHE_KEY,
                [self._serialise(m) for m in models],
                ttl=_PROBE_TTL,
            )
        except Exception:  # pragma: no cover
            pass
        return models

    @staticmethod
    def _serialise(m: InstalledModel) -> dict[str, Any]:
        return {
            "tag": m.tag,
            "size_bytes": m.size_bytes,
            "modified_at": m.modified_at,
            "is_custom": m.is_custom,
            "display_name": m.display_name,
        }

    @staticmethod
    def _inflate_cached(d: dict[str, Any]) -> InstalledModel:
        # Re-match catalogue on read since ModelSpec isn't JSON-friendly.
        tag = str(d.get("tag") or "")
        spec = next(
            (s for s in CODE_MODEL_CATALOGUE if s.ollama_tag.lower() == tag.lower()),
            None,
        )
        return InstalledModel(
            tag=tag,
            size_bytes=int(d.get("size_bytes") or 0),
            modified_at=str(d.get("modified_at") or ""),
            is_custom=bool(d.get("is_custom")),
            display_name=d.get("display_name"),
            spec=spec,
        )

    @staticmethod
    def _scan_custom_tags() -> set[str]:
        """Walk ``CUSTOM_MODELS_DIR`` for .meta.json sidecars; return tags."""
        if not CUSTOM_MODELS_DIR.exists():
            return set()
        out: set[str] = set()
        try:
            for meta_file in CUSTOM_MODELS_DIR.glob("**/*.meta.json"):
                try:
                    meta = json.loads(meta_file.read_text(encoding="utf-8"))
                    tag = str(meta.get("tag") or "").lower()
                    if tag:
                        out.add(tag)
                except Exception:  # pragma: no cover
                    pass
        except Exception:  # pragma: no cover
            pass
        return out

    # ── 2. AUTO-SELECT ────────────────────────────────────────────────────

    async def auto_select(
        self,
        mode: str = "__all__",
        effort: str = "medium",
    ) -> tuple[str, str]:
        """Return ``(tag, reason)`` for the best installed model.

        Scoring (max ≈ 100):
          - Tier fit          0–30
          - Strength match    0–20
          - SWE-bench         0–30
          - HumanEval         0–10
          - Context ≥ 32k     0–10
        """
        installed = await self.list_installed()
        if not installed:
            return (
                OLLAMA_MODEL_DEFAULT,
                "default — no models installed yet; will auto-pull",
            )

        tier_pref = EFFORT_TIER_MAP.get(
            effort, ["balanced", "flagship", "lightweight"]
        )
        required = MODE_REQUIREMENTS.get(mode, MODE_REQUIREMENTS["__all__"])

        scored: list[tuple[float, InstalledModel]] = []
        unmatched: list[InstalledModel] = []

        for m in installed:
            spec = m.spec
            if spec is None:
                unmatched.append(m)
                continue

            score = 0.0
            if spec.tier in tier_pref:
                idx = tier_pref.index(spec.tier)
                score += ((len(tier_pref) - idx) / len(tier_pref)) * 30
            matched = sum(1 for s in required if s in spec.strengths)
            score += (matched / max(len(required), 1)) * 20
            score += min(spec.swebench_pct / 100.0, 1.0) * 30
            score += min(spec.humaneval_pct / 100.0, 1.0) * 10
            if spec.context_k >= 32:
                score += 10
            scored.append((score, m))

        if scored:
            best_score, best = max(scored, key=lambda x: x[0])
            label = best.display_name or best.tag
            return (
                best.tag,
                f"auto-selected — best fit for {mode}/{effort} ({label})",
            )

        if unmatched:
            biggest = max(unmatched, key=lambda m: m.size_bytes)
            return (
                biggest.tag,
                f"auto-selected — largest available ({biggest.tag})",
            )

        return (
            installed[0].tag,
            f"auto-selected — first available ({installed[0].tag})",
        )

    # ── 3. RESOLVE EFFECTIVE MODEL ────────────────────────────────────────

    async def resolve_model(
        self,
        *,
        user_id: str | None,
        client_id: str,
        mode: str,
        effort: str = "medium",
    ) -> tuple[str, str]:
        """Resolution order:
        1. User preference for this exact mode
        2. User preference for "__all__"
        3. Auto-select
        """
        try:
            pref = await self._store.get_model_preference(
                user_id=user_id, client_id=client_id, mode=mode,
            )
            if pref:
                return (pref, f"user preference ({mode})")
        except Exception as exc:
            logger.warning("model_manager_pref_lookup_failed: %s", exc)

        return await self.auto_select(mode=mode, effort=effort)

    # ── 4. PULL FROM OLLAMA HUB ───────────────────────────────────────────

    async def pull_model_stream(
        self, tag: str,
    ) -> AsyncIterator[dict[str, Any]]:
        """Stream Ollama pull progress as event dicts."""
        yield {"type": "pull_start", "tag": tag}
        try:
            async with httpx.AsyncClient(timeout=None) as client:
                async with client.stream(
                    "POST",
                    f"{OLLAMA_BASE_URL}/api/pull",
                    json={"name": tag, "stream": True},
                ) as resp:
                    resp.raise_for_status()
                    async for raw in resp.aiter_lines():
                        if not raw.strip():
                            continue
                        try:
                            chunk = json.loads(raw)
                        except json.JSONDecodeError:
                            continue
                        if chunk.get("error"):
                            yield {
                                "type": "pull_error",
                                "tag": tag,
                                "error": str(chunk["error"]),
                            }
                            return
                        completed = int(chunk.get("completed") or 0)
                        total = int(chunk.get("total") or 0)
                        pct = int(completed / total * 100) if total > 0 else 0
                        yield {
                            "type": "pull_progress",
                            "tag": tag,
                            "status": str(chunk.get("status") or ""),
                            "pct": pct,
                            "bytes_done": completed,
                            "bytes_total": total,
                        }
            with contextlib.suppress(Exception):
                await cache_manager.delete(_PROBE_CACHE_KEY)
            yield {"type": "pull_complete", "tag": tag}
        except Exception as exc:
            yield {"type": "pull_error", "tag": tag, "error": str(exc)}

    # ── 5. UPLOAD GGUF ────────────────────────────────────────────────────

    async def import_gguf(
        self,
        *,
        user_id: str | None,
        client_id: str,
        filename: str,
        file_bytes: bytes,
        display_name: str | None = None,
    ) -> dict[str, Any]:
        """Validate + persist + register a user-uploaded GGUF."""
        if len(file_bytes) < 4 or file_bytes[:4] != b"GGUF":
            raise ValueError(
                "File does not appear to be a valid GGUF model "
                "(missing GGUF magic bytes).",
            )
        if len(file_bytes) > MAX_UPLOAD_SIZE_BYTES:
            gb = len(file_bytes) / (1024**3)
            limit_gb = MAX_UPLOAD_SIZE_BYTES // (1024**3)
            raise ValueError(
                f"File is {gb:.1f} GB — exceeds the {limit_gb} GB upload limit.",
            )

        # Stable tag from filename + first-64KB hash. The `custom/`
        # namespace prefix prevents collisions with official Ollama tags.
        safe_stem = "".join(
            c if c.isalnum() or c in "-_." else "_"
            for c in Path(filename).stem[:40]
        ).lower().strip("_-.") or "model"
        file_hash = hashlib.sha256(file_bytes[:65536]).hexdigest()[:8]
        tag = f"custom/{safe_stem}:{file_hash}"

        owner = (user_id or client_id or "anonymous").replace("/", "_")[:40]
        dest_dir = CUSTOM_MODELS_DIR / owner
        dest_dir.mkdir(parents=True, exist_ok=True)

        gguf_path = dest_dir / f"{safe_stem}_{file_hash}.gguf"
        gguf_path.write_bytes(file_bytes)
        logger.info(
            "model_manager_gguf_saved owner=%s path=%s bytes=%d",
            owner, gguf_path, len(file_bytes),
        )

        display = display_name or safe_stem.replace("_", " ").title()
        modelfile = (
            f"FROM {gguf_path}\n"
            "PARAMETER temperature 0.7\n"
            "PARAMETER top_p 0.9\n"
            "PARAMETER num_ctx 4096\n"
        )
        modelfile_path = dest_dir / f"{safe_stem}_{file_hash}.Modelfile"
        modelfile_path.write_text(modelfile, encoding="utf-8")

        # Register with Ollama via the HTTP /api/create endpoint. The
        # daemon parses the Modelfile string we pass and reads the
        # FROM-referenced GGUF directly off disk — that's why both the
        # app container *and* the Ollama container must have the same
        # CUSTOM_MODELS_DIR volume mount. Streams JSONL status updates;
        # we drain to completion (10-min cap to match the old CLI path).
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(600.0)) as client:
                async with client.stream(
                    "POST",
                    f"{OLLAMA_BASE_URL}/api/create",
                    json={"name": tag, "modelfile": modelfile, "stream": True},
                ) as resp:
                    resp.raise_for_status()
                    last_status = ""
                    async for raw in resp.aiter_lines():
                        if not raw.strip():
                            continue
                        try:
                            chunk = json.loads(raw)
                        except json.JSONDecodeError:
                            continue
                        if chunk.get("error"):
                            raise RuntimeError(
                                f"ollama /api/create rejected the model: "
                                f"{str(chunk['error'])[:500]}",
                            )
                        if chunk.get("status"):
                            last_status = str(chunk["status"])
                            logger.debug(
                                "model_manager_create status=%s", last_status,
                            )
        except httpx.HTTPStatusError as exc:
            body = exc.response.text[:500]
            raise RuntimeError(
                f"ollama /api/create failed (HTTP {exc.response.status_code}): {body}",
            ) from exc
        except httpx.RequestError as exc:
            raise RuntimeError(
                f"ollama /api/create unreachable: {exc}",
            ) from exc

        # Sidecar metadata so we can list + delete custom models later.
        meta = {
            "tag": tag,
            "display_name": display,
            "original_filename": filename,
            "owner": owner,
            "gguf_path": str(gguf_path),
            "modelfile_path": str(modelfile_path),
            "size_bytes": len(file_bytes),
        }
        meta_path = dest_dir / f"{safe_stem}_{file_hash}.meta.json"
        meta_path.write_text(json.dumps(meta, indent=2), encoding="utf-8")

        with contextlib.suppress(Exception):
            await cache_manager.delete(_PROBE_CACHE_KEY)
        logger.info("model_manager_gguf_registered tag=%s", tag)
        return {"tag": tag, "display_name": display}

    # ── 6. DELETE CUSTOM MODEL ────────────────────────────────────────────

    async def delete_custom_model(
        self,
        *,
        tag: str,
        user_id: str | None,
        client_id: str,
    ) -> None:
        """Remove a custom-uploaded model + its sidecar files. Owner-only."""
        owner = (user_id or client_id or "anonymous").replace("/", "_")[:40]
        owner_dir = CUSTOM_MODELS_DIR / owner
        if not owner_dir.exists():
            raise PermissionError(
                f"Model '{tag}' does not belong to this user.",
            )

        meta_files: list[Path] = []
        for meta_file in owner_dir.glob("*.meta.json"):
            try:
                meta = json.loads(meta_file.read_text(encoding="utf-8"))
                if meta.get("tag") == tag:
                    meta_files.append(meta_file)
            except Exception:  # pragma: no cover
                pass

        if not meta_files:
            raise PermissionError(
                f"Model '{tag}' does not belong to this user.",
            )

        # Delete from Ollama (404 = already gone, fine).
        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                resp = await client.request(
                    "DELETE",
                    f"{OLLAMA_BASE_URL}/api/delete",
                    json={"name": tag},
                )
                if resp.status_code not in (200, 404):
                    resp.raise_for_status()
        except Exception as exc:
            logger.warning("model_manager_ollama_delete_failed: %s", exc)

        # Remove filesystem artefacts.
        for meta_file in meta_files:
            try:
                meta = json.loads(meta_file.read_text(encoding="utf-8"))
                gp = meta.get("gguf_path")
                mp = meta.get("modelfile_path")
                if gp:
                    Path(gp).unlink(missing_ok=True)
                if mp:
                    Path(mp).unlink(missing_ok=True)
                meta_file.unlink(missing_ok=True)
            except Exception as exc:  # pragma: no cover
                logger.warning("model_manager_cleanup_failed: %s", exc)

        with contextlib.suppress(Exception):
            await cache_manager.delete(_PROBE_CACHE_KEY)
        logger.info("model_manager_custom_deleted tag=%s owner=%s", tag, owner)
