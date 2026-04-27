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

    # ── 7. SEARCH MODELS (v3 — Discover tab) ─────────────────────────────

    async def search_models(
        self,
        query: str,
        *,
        source: str = "all",
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        """
        Search for installable models matching ``query``. Returns a list
        of dicts shaped like:

            {
              "tag": "qwen2.5:7b" | "hf.co/Org/Model:Q4_K_M",
              "display_name": "Qwen2.5 7B",
              "source": "ollama_curated" | "hf",
              "description": "...",
              "license": "Apache-2.0",
              "size_bytes": 4400000000,
              "stars": 1234,
              "downloads": 56789,
              "spec": {...} | None,   # CodeModelRegistry match if known
            }

        ``source`` is one of:
          - ``ollama_curated`` — search just the curated CODE_MODEL_CATALOGUE
          - ``hf``             — search Hugging Face Hub for GGUF models
          - ``all``             — both, deduped

        Failure-quiet: HF unreachable → returns just the curated matches.
        """
        q = (query or "").strip().lower()
        if not q:
            return []
        results: list[dict[str, Any]] = []
        seen_tags: set[str] = set()

        # 1) Curated catalogue — fast in-process filter.
        if source in {"all", "ollama_curated"}:
            for spec in CODE_MODEL_CATALOGUE:
                hay = " ".join([
                    spec.ollama_tag, spec.display_name,
                    " ".join(spec.strengths), spec.tier, spec.license,
                ]).lower()
                if q in hay:
                    if spec.ollama_tag.lower() in seen_tags:
                        continue
                    seen_tags.add(spec.ollama_tag.lower())
                    results.append({
                        "tag": spec.ollama_tag,
                        "display_name": spec.display_name,
                        "source": "ollama_curated",
                        "description": (
                            f"{spec.tier.title()} tier · {spec.params_b}B params · "
                            f"{spec.context_k}k context · {spec.license}"
                        ),
                        "license": spec.license,
                        "size_bytes": int(spec.vram_gb * 1024**3),
                        "stars": None,
                        "downloads": None,
                        "spec": spec.to_dict(),
                    })

        # 2) Hugging Face Hub — only when GGUF library exists. We
        # intentionally don't pull `huggingface_hub` (heavy dep) — the
        # public REST API works fine over httpx.
        if source in {"all", "hf"} and len(results) < limit:
            try:
                async with httpx.AsyncClient(timeout=10.0) as client:
                    resp = await client.get(
                        "https://huggingface.co/api/models",
                        params={
                            "search": query,
                            "filter": "gguf",
                            "limit": str(min(limit, 50)),
                            "sort": "downloads",
                            "direction": "-1",
                        },
                    )
                    resp.raise_for_status()
                    raw = resp.json()
            except Exception as exc:
                logger.warning("model_manager_hf_search_failed: %s", exc)
                raw = []

            for entry in (raw or []):
                model_id = str(entry.get("id") or entry.get("modelId") or "")
                if not model_id:
                    continue
                # Default Ollama hf.co tag uses Q4_K_M as a reasonable
                # quant; the picker can offer alt quants in a follow-up.
                tag = f"hf.co/{model_id}:Q4_K_M"
                if tag.lower() in seen_tags:
                    continue
                seen_tags.add(tag.lower())
                results.append({
                    "tag": tag,
                    "display_name": model_id.split("/")[-1],
                    "source": "hf",
                    "description": (entry.get("pipeline_tag") or "GGUF model"),
                    "license": (
                        (entry.get("cardData") or {}).get("license")
                        if isinstance(entry.get("cardData"), dict)
                        else None
                    ),
                    "size_bytes": None,
                    "stars": int(entry.get("likes") or 0),
                    "downloads": int(entry.get("downloads") or 0),
                    "spec": None,
                })
                if len(results) >= limit:
                    break

        return results[:limit]

    # ── 8. HARDWARE DETECTION (v3) ───────────────────────────────────────

    async def detect_hardware(self) -> dict[str, Any]:
        """
        Probe Ollama for runtime hardware info. Returns a dict shaped:

            {
              "gpu_available": bool,
              "gpu_name": str | None,
              "gpu_count": int,
              "vram_total_gb": float | None,
              "vram_free_gb": float | None,
              "cpu_threads": int | None,
              "ollama_version": str | None,
              "platform": str | None,
            }

        Ollama exposes /api/version and /api/ps; the latter includes
        loaded-model size. We additionally query Python's os.cpu_count
        for a CPU-thread baseline.
        """
        out: dict[str, Any] = {
            "gpu_available": False,
            "gpu_name": None,
            "gpu_count": 0,
            "vram_total_gb": None,
            "vram_free_gb": None,
            "cpu_threads": os.cpu_count(),
            "ollama_version": None,
            "platform": None,
        }
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                # Version + platform.
                v = await client.get(f"{OLLAMA_BASE_URL}/api/version")
                if v.status_code == 200:
                    j = v.json()
                    out["ollama_version"] = str(j.get("version") or "")
                # /api/ps gives currently-loaded models with size + GPU info.
                p = await client.get(f"{OLLAMA_BASE_URL}/api/ps")
                if p.status_code == 200:
                    pj = p.json()
                    models = pj.get("models") or []
                    # If at least one model has details.size_vram > 0,
                    # the host is GPU-capable (Ollama populates it only
                    # when the model is on GPU).
                    for m in models:
                        details = m.get("details") or {}
                        # Newer Ollama: top-level size_vram
                        if int(m.get("size_vram") or 0) > 0:
                            out["gpu_available"] = True
                            out["gpu_count"] = max(out["gpu_count"], 1)
                        # Legacy: details.size
                        if isinstance(details, dict) and details.get("families"):
                            out["platform"] = ",".join(details["families"])
        except Exception as exc:
            logger.warning("model_manager_hardware_probe_failed: %s", exc)

        # Best-effort GPU detection beyond Ollama — pynvml if installed,
        # otherwise CUDA_VISIBLE_DEVICES env hint.
        try:
            import pynvml  # type: ignore  # noqa: PLC0415
            pynvml.nvmlInit()
            count = pynvml.nvmlDeviceGetCount()
            if count > 0:
                out["gpu_available"] = True
                out["gpu_count"] = max(out["gpu_count"], int(count))
                handle = pynvml.nvmlDeviceGetHandleByIndex(0)
                out["gpu_name"] = pynvml.nvmlDeviceGetName(handle)
                if isinstance(out["gpu_name"], bytes):
                    out["gpu_name"] = out["gpu_name"].decode("utf-8", "ignore")
                meminfo = pynvml.nvmlDeviceGetMemoryInfo(handle)
                out["vram_total_gb"] = round(meminfo.total / 1024**3, 2)
                out["vram_free_gb"] = round(meminfo.free / 1024**3, 2)
            with contextlib.suppress(Exception):
                pynvml.nvmlShutdown()
        except Exception:  # pragma: no cover — pynvml missing or no CUDA
            cuda_env = os.getenv("CUDA_VISIBLE_DEVICES")
            if cuda_env and cuda_env.strip() not in {"", "-1", "none"}:
                out["gpu_available"] = True
                out["gpu_count"] = max(
                    out["gpu_count"],
                    len([s for s in cuda_env.split(",") if s.strip()]),
                )
        return out

    # ── 8a. SMART RECOMMENDATION (v4) ────────────────────────────────────

    # Keywords → role hints; used by recommend_for_prompt to bias the
    # mode away from a pure "research / thinking / code" tag and onto
    # specific strengths (debugging, agentic, math, etc.). Tuned by hand
    # — small enough to be readable, broad enough to land most prompts.
    _RECOMMEND_KEYWORDS: dict[str, list[str]] = {
        "code generation": [
            "implement", "write a function", "build a", "code", "module",
            "class ", "def ", "function", "script", "snippet", "endpoint",
        ],
        "debugging": [
            "fix", "debug", "error", "exception", "traceback", "stack trace",
            "broken", "doesn't work", "doesnt work", "why isn't",
        ],
        "explanation": [
            "explain", "what does", "how does", "describe", "summarize",
            "summarise", "tldr", "tl;dr", "overview",
        ],
        "math": [
            "calculate", "compute", "math", "equation", "integral",
            "derivative", "matrix", "vector", "probability",
        ],
        "agentic loops": [
            "agent", "loop until", "iterate until", "self-correct",
            "reasoning chain", "step by step",
        ],
        "multi-file editing": [
            "across files", "refactor the project", "whole codebase",
            "every occurrence", "global rename",
        ],
        "planning": [
            "plan", "outline", "roadmap", "design", "blueprint", "approach",
        ],
        "fast inference": [
            "quick", "fast", "short", "one-liner", "snappy",
        ],
    }

    async def recommend_for_prompt(
        self,
        prompt: str,
        *,
        mode: str = "__all__",
        usage: dict[str, Any] | None = None,
        hardware: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """
        Recommend a model for ``prompt``. Pure heuristic — no LLM call.

        Strategy:
          1. Score every installed model on:
              · keyword → strength match (40% weight)
              · mode-baseline strength match (15%)
              · SWE-bench / HumanEval (25%)
              · VRAM fit on the detected GPU (15%)
              · usage history (5% — favour the user's regulars)
          2. Return the top candidate + its score + a short reason
             string suitable for an inline banner ("DeepSeek-Coder
             6.7B · best fit for debugging on this GPU").
        """
        installed = await self.list_installed()
        if not installed:
            tag, reason = await self.auto_select(mode=mode)
            return {
                "tag": tag, "reason": reason, "score": 0,
                "candidates": [],
            }

        # Detect prompt strengths via keyword sweep.
        prompt_lc = (prompt or "").lower()
        prompt_strengths: set[str] = set()
        for strength, kws in self._RECOMMEND_KEYWORDS.items():
            if any(kw in prompt_lc for kw in kws):
                prompt_strengths.add(strength)

        # Mode baseline strengths (research/thinking/code).
        mode_baseline = set(MODE_REQUIREMENTS.get(mode, MODE_REQUIREMENTS["__all__"]))

        if hardware is None:
            try:
                hardware = await self.detect_hardware()
            except Exception:  # pragma: no cover
                hardware = {}
        vram_total = (hardware or {}).get("vram_total_gb")
        vram_free = (hardware or {}).get("vram_free_gb")

        usage_map = usage or {}

        scored: list[tuple[float, dict[str, Any]]] = []
        for m in installed:
            spec = m.spec
            if spec is None:
                continue
            score = 0.0
            # 1) keyword strength match — 40%
            kw_match = sum(1 for s in prompt_strengths if s in spec.strengths)
            if prompt_strengths:
                score += (kw_match / max(len(prompt_strengths), 1)) * 40
            # 2) mode baseline match — 15%
            base_match = sum(1 for s in mode_baseline if s in spec.strengths)
            if mode_baseline:
                score += (base_match / max(len(mode_baseline), 1)) * 15
            # 3) benchmarks — 25% (SWE-bench dominates code, HumanEval bonus)
            score += min(spec.swebench_pct / 100.0, 1.0) * 18
            score += min(spec.humaneval_pct / 100.0, 1.0) * 7
            # 4) VRAM fit — 15% (penalise too-big, reward fits)
            vram_req = self.estimate_vram_gb(m)
            fit = self.fit_classification(vram_req, vram_total, vram_free)
            score += {"fits": 15, "tight": 8, "cpu": 5,
                      "unknown": 5, "too_big": -10}.get(fit, 0)
            # 5) usage signal — 5% (log-scale; first use ≈ 1 point)
            count = int((usage_map.get(m.tag) or {}).get("count_total") or 0)
            if count > 0:
                import math  # noqa: PLC0415
                score += min(math.log1p(count) * 1.5, 5)
            scored.append((score, {
                "tag": m.tag,
                "display_name": m.display_name or m.tag,
                "score": round(score, 2),
                "strengths_matched": [
                    s for s in prompt_strengths if s in spec.strengths
                ],
                "fit": fit,
                "vram_required_gb": vram_req,
                "usage_count": count,
            }))

        scored.sort(key=lambda x: x[0], reverse=True)
        if not scored:
            tag, reason = await self.auto_select(mode=mode)
            return {"tag": tag, "reason": reason, "score": 0, "candidates": []}

        top_score, top = scored[0]
        # Build a short, human-readable reason.
        bits: list[str] = []
        if top["strengths_matched"]:
            bits.append("matches " + ", ".join(top["strengths_matched"][:2]))
        if top["fit"] == "fits":
            bits.append("fits comfortably")
        elif top["fit"] == "tight":
            bits.append("fits — tight on VRAM")
        elif top["fit"] == "too_big":
            bits.append("⚠ may not fit on this GPU")
        if top["usage_count"] > 5:
            bits.append(f"used {top['usage_count']}× before")
        reason = " · ".join(bits) or "best-scored installed model"

        return {
            "tag": top["tag"],
            "display_name": top["display_name"],
            "score": round(top_score, 2),
            "reason": reason,
            "candidates": [c for _, c in scored[:5]],
            "detected_strengths": sorted(prompt_strengths),
        }

    # ── 8b. VRAM FIT + WARMUP (v4) ───────────────────────────────────────

    @staticmethod
    def estimate_vram_gb(
        model: dict[str, Any] | InstalledModel,
    ) -> float | None:
        """Best-effort VRAM estimate (in GB) for a tag.

        Order:
          1. ``size_bytes`` from Ollama (most accurate — disk size ≈ Q4
             VRAM footprint within ±15%).
          2. ``spec.vram_gb`` from the curated catalogue.
          3. None — caller renders an "unknown" badge.
        """
        # Accept either an InstalledModel dataclass or a dict envelope.
        if isinstance(model, InstalledModel):
            size_bytes = model.size_bytes
            spec = model.spec
        else:
            size_bytes = int(model.get("size_bytes") or 0)
            spec_dict = model.get("spec") or {}
            spec = spec_dict if spec_dict else None  # may be a dict, not a ModelSpec

        if size_bytes:
            return round(size_bytes / 1024**3, 2)
        if spec is None:
            return None
        # Allow either a ModelSpec instance or a plain dict.
        vram = getattr(spec, "vram_gb", None)
        if vram is None and isinstance(spec, dict):
            vram = spec.get("vram_gb")
        try:
            return float(vram) if vram is not None else None
        except (TypeError, ValueError):
            return None

    @staticmethod
    def fit_classification(
        vram_required_gb: float | None,
        vram_total_gb: float | None,
        vram_free_gb: float | None = None,
    ) -> str:
        """Classify how comfortably a model will fit on the detected GPU.

        Returns one of: ``"unknown"``, ``"fits"`` (≤ 70% of free / 60%
        of total), ``"tight"`` (≤ 100% of total), ``"too_big"`` (above).
        Tuned to be conservative — KV cache + context overhead can add
        20-30% on top of the model's static footprint.
        """
        if vram_required_gb is None:
            return "unknown"
        # No GPU detected → CPU-only (renders as "cpu" so the UI can
        # use a different colour; not technically a "fits/tight" call).
        if not vram_total_gb:
            return "cpu"
        # Prefer free VRAM when we have it (live state); else 60% of total.
        budget = vram_free_gb if vram_free_gb is not None else (vram_total_gb * 0.85)
        if vram_required_gb <= budget * 0.85:
            return "fits"
        if vram_required_gb <= vram_total_gb:
            return "tight"
        return "too_big"

    async def warmup_model(self, tag: str) -> bool:
        """
        Pre-load a model into VRAM by issuing a 1-token generation with
        ``keep_alive=10m``. Idempotent — Ollama already keeps loaded
        models hot, so a second call is essentially free.

        Called by the picker when the user clicks Save Profile so the
        first real request doesn't pay the 5-30s cold-load cost.
        """
        try:
            async with httpx.AsyncClient(timeout=120.0) as client:
                resp = await client.post(
                    f"{OLLAMA_BASE_URL}/api/generate",
                    json={
                        "model": tag,
                        "prompt": "",
                        "stream": False,
                        "keep_alive": "10m",
                        "options": {"num_predict": 1},
                    },
                )
                resp.raise_for_status()
            return True
        except Exception as exc:
            logger.warning("model_warmup_failed tag=%s err=%s", tag, exc)
            return False

    # ── 9. PROFILE → OLLAMA OPTIONS ──────────────────────────────────────

    @staticmethod
    def apply_profile_to_options(
        profile: dict[str, Any] | None,
    ) -> dict[str, Any]:
        """
        Translate a saved profile dict to Ollama ``/api/generate``
        ``options`` keys. Whitelisted to avoid passing arbitrary
        client-supplied keys to the daemon.
        """
        if not profile:
            return {}
        whitelist = {
            "temperature": float,
            "top_p": float,
            "top_k": int,
            "repeat_penalty": float,
            "num_ctx": int,
            "num_gpu": int,
            "num_thread": int,
            "seed": int,
            "mirostat": int,
            "mirostat_tau": float,
            "mirostat_eta": float,
            "num_predict": int,
        }
        out: dict[str, Any] = {}
        for k, caster in whitelist.items():
            if k in profile and profile[k] is not None:
                with contextlib.suppress(TypeError, ValueError):
                    out[k] = caster(profile[k])
        # `stop` is a list of strings — handled separately.
        if isinstance(profile.get("stop"), list):
            out["stop"] = [str(s) for s in profile["stop"] if s][:8]
        return out

    @staticmethod
    def system_prompt_from_profile(
        profile: dict[str, Any] | None,
    ) -> str | None:
        """Extract the user-defined system prompt from a profile dict."""
        if not profile:
            return None
        sp = profile.get("system_prompt")
        if isinstance(sp, str) and sp.strip():
            # Cap at 4 KB to avoid prompt-bombing.
            return sp.strip()[:4096]
        return None

    # ── 10. TEST GENERATION (v3 — "Try it" button) ───────────────────────

    async def test_generate(
        self,
        *,
        model: str,
        prompt: str,
        profile: dict[str, Any] | None = None,
        max_tokens: int = 256,
    ) -> AsyncIterator[dict[str, Any]]:
        """
        Stream a tiny test generation. Yields event dicts:

          - {"type": "test_start", "model": "..."}
          - {"type": "test_chunk", "delta": "..."}
          - {"type": "test_done",  "elapsed_ms": int, "tokens": int}
          - {"type": "test_error", "error": "..."}
        """
        yield {"type": "test_start", "model": model}
        opts = self.apply_profile_to_options(profile)
        opts["num_predict"] = int(min(max_tokens, 1024))
        body: dict[str, Any] = {
            "model": model,
            "prompt": str(prompt or "")[:8000],
            "stream": True,
            "options": opts,
        }
        sysp = self.system_prompt_from_profile(profile)
        if sysp:
            body["system"] = sysp

        import time as _time  # noqa: PLC0415
        started = _time.time()
        tokens = 0
        # v4 — live tokens-per-second telemetry. We emit every chunk
        # but only attach a tok/s estimate every 8 chunks so the UI
        # doesn't flicker on each token (and the JS doesn't have to
        # smooth a noisy signal).
        last_telemetry_token = 0
        try:
            async with httpx.AsyncClient(timeout=120.0) as client:
                async with client.stream(
                    "POST",
                    f"{OLLAMA_BASE_URL}/api/generate",
                    json=body,
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
                                "type": "test_error",
                                "error": str(chunk["error"]),
                            }
                            return
                        delta = chunk.get("response") or ""
                        if delta:
                            tokens += 1
                            elapsed = max(_time.time() - started, 1e-3)
                            chunk_evt: dict[str, Any] = {
                                "type": "test_chunk",
                                "delta": delta,
                                "tokens": tokens,
                            }
                            # Telemetry frame every 8 tokens.
                            if tokens - last_telemetry_token >= 8:
                                chunk_evt["tokens_per_second"] = round(
                                    tokens / elapsed, 2,
                                )
                                chunk_evt["elapsed_ms"] = int(elapsed * 1000)
                                last_telemetry_token = tokens
                            yield chunk_evt
                        if chunk.get("done"):
                            break
            elapsed_ms = int((_time.time() - started) * 1000)
            yield {
                "type": "test_done",
                "elapsed_ms": elapsed_ms,
                "tokens": tokens,
                "tokens_per_second": round(
                    tokens / max(elapsed_ms / 1000.0, 1e-3), 2,
                ) if tokens else 0.0,
                # Pass through Ollama's per-eval timing if the daemon
                # reported it (more accurate than wall-clock).
                "eval_count": chunk.get("eval_count"),
                "eval_duration_ns": chunk.get("eval_duration"),
            }
        except Exception as exc:
            yield {"type": "test_error", "error": str(exc)}

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
