"""
Cycle C Sprint 1 Day 4 — admin LLM dashboard endpoints.

GET  /api/admin/llm                    — current backend + (if llama-swap)
                                          resident models, swap events,
                                          cache-reuse hits, recent
                                          completion timings.
GET  /api/admin/llm/models             — flat model list (resolves regardless
                                          of backend).
POST /api/admin/llm/swap-to/{model_id} — kick a swap by querying llama-swap's
                                          /v1/models then triggering a tiny
                                          completion to force the load.

Driven by ``settings.llm_backend`` / ``AMOR_LLM_BACKEND`` env (Cycle C
Sprint 1 Day 1 wired the env-driven rollback flag).  When backend is
``ollama`` we fall back to the Ollama-flavored payload (load_status,
keep_alive, models loaded).

Auth: every endpoint requires ``get_current_user``.  Single-tenant
project — admin role gate is a future Sprint 12 concern.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from collections import deque
from typing import Any, Dict, List, Optional

import httpx
from fastapi import APIRouter, Depends, HTTPException, status

from ..auth.dependencies import get_current_user
from ..auth.models import User
from ..config.settings import settings

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/admin/llm", tags=["admin-llm"])


# ─── tiny in-process telemetry ring buffers ────────────────────────


# Last N successful completion timings (per backend); used by the UI to
# show p50/p95 first-token latency.  Unbounded would leak memory; 200
# is enough for a meaningful percentile yet trivial in RAM.
_TIMING_WINDOW: int = 200
_TIMINGS: deque[Dict[str, Any]] = deque(maxlen=_TIMING_WINDOW)
_SWAP_EVENTS: deque[Dict[str, Any]] = deque(maxlen=50)
_CACHE_REUSE_HITS: int = 0


def record_completion_timing(
    *,
    backend: str,
    model: str,
    duration_ms: float,
    prompt_tokens: int,
    completion_tokens: int,
    cache_reuse_hit: bool = False,
) -> None:
    """Hook called from the LLM backend bridge — best-effort, never
    raises (a telemetry hiccup must not break the inference path)."""
    try:
        _TIMINGS.append(
            {
                "backend": backend,
                "model": model,
                "duration_ms": float(duration_ms),
                "prompt_tokens": int(prompt_tokens),
                "completion_tokens": int(completion_tokens),
                "cache_reuse_hit": bool(cache_reuse_hit),
                "ts": time.time(),
            },
        )
        if cache_reuse_hit:
            global _CACHE_REUSE_HITS
            _CACHE_REUSE_HITS += 1
    except Exception:
        pass


def record_swap_event(
    *,
    from_model: Optional[str],
    to_model: str,
    cold_load_ms: float,
) -> None:
    try:
        _SWAP_EVENTS.append(
            {
                "from": from_model,
                "to": to_model,
                "cold_load_ms": float(cold_load_ms),
                "ts": time.time(),
            },
        )
    except Exception:
        pass


def _percentile(values: List[float], pct: float) -> Optional[float]:
    if not values:
        return None
    s = sorted(values)
    k = max(0, min(len(s) - 1, int(round(pct / 100.0 * (len(s) - 1)))))
    return s[k]


def _resolve_active_backend() -> str:
    """Mirror local_ai.llm_backend._resolve_kind without the
    side-effect of constructing a backend instance."""
    kind = (getattr(settings, "llm_backend", None) or "").strip().lower()
    if kind:
        return kind
    return os.environ.get("AMOR_LLM_BACKEND", "ollama").strip().lower()


def _llamaswap_base_url() -> str:
    """Where the llama-swap proxy lives.  Inside docker-compose the app
    talks to the service by name on the shared network."""
    fallback = os.environ.get(
        "AMOR_LLAMASWAP_URL",
        "http://amor-llama-swap:9100",
    )
    return getattr(settings, "llm_backend_url", "").strip() or fallback


# ─── llama-swap probe ──────────────────────────────────────────────


async def _probe_llamaswap() -> Dict[str, Any]:
    """Reach into llama-swap's diagnostics: /v1/models gives the
    declared model list; /running gives currently-resident."""
    base = _llamaswap_base_url().rstrip("/")
    out: Dict[str, Any] = {
        "base_url": base,
        "healthy": False,
        "declared_models": [],
        "resident_models": [],
        "error": None,
    }
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            try:
                h = await client.get(base + "/health")
                out["healthy"] = h.status_code == 200
            except Exception as exc:
                out["error"] = f"health: {exc}"
                return out
            try:
                m = await client.get(base + "/v1/models")
                if m.status_code == 200:
                    body = m.json()
                    out["declared_models"] = [
                        {
                            "id": item.get("id"),
                            "name": item.get("name") or item.get("id"),
                        }
                        for item in (body.get("data") or [])
                    ]
            except Exception as exc:
                out["error"] = f"models: {exc}"
            try:
                # llama-swap exposes /running in v110+; older builds
                # 404 here — we just leave resident_models empty.
                r = await client.get(base + "/running")
                if r.status_code == 200:
                    body = r.json()
                    if isinstance(body, list):
                        out["resident_models"] = body
                    elif isinstance(body, dict):
                        out["resident_models"] = body.get("running") or []
            except Exception:
                pass
    except Exception as exc:
        out["error"] = str(exc)
    return out


async def _probe_ollama() -> Dict[str, Any]:
    base = os.environ.get("OLLAMA_BASE_URL", "http://amor-ollama:11434")
    out: Dict[str, Any] = {
        "base_url": base,
        "healthy": False,
        "declared_models": [],
        "resident_models": [],
        "error": None,
    }
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            try:
                t = await client.get(base + "/api/tags")
                if t.status_code == 200:
                    out["healthy"] = True
                    body = t.json()
                    out["declared_models"] = [
                        {"id": m.get("name"), "name": m.get("name")}
                        for m in (body.get("models") or [])
                    ]
            except Exception as exc:
                out["error"] = f"tags: {exc}"
            try:
                p = await client.get(base + "/api/ps")
                if p.status_code == 200:
                    body = p.json()
                    out["resident_models"] = [
                        {
                            "id": m.get("name"),
                            "name": m.get("name"),
                            "size_vram": m.get("size_vram"),
                        }
                        for m in (body.get("models") or [])
                    ]
            except Exception:
                pass
    except Exception as exc:
        out["error"] = str(exc)
    return out


# ─── handlers ──────────────────────────────────────────────────────


@router.get("")
async def get_llm_state(
    user: User = Depends(get_current_user),
) -> Dict[str, Any]:
    """Backend kind + service-specific resident-model probe + last-100
    completion timing percentiles + recent swap events.

    Single endpoint so the UI can render a one-glance dashboard
    without coordinating multiple fetches.
    """
    backend = _resolve_active_backend()
    if backend in {"llama-cpp", "llama_cpp", "llamacpp", "llama.cpp",
                   "llama-swap", "llama_swap", "llamaswap"}:
        probe = await _probe_llamaswap()
        backend_label = "llama-swap"
    elif backend == "ollama":
        probe = await _probe_ollama()
        backend_label = "ollama"
    else:
        probe = {
            "base_url": getattr(settings, "llm_backend_url", "") or "",
            "healthy": None,
            "declared_models": [],
            "resident_models": [],
            "error": f"backend kind {backend!r} has no live probe",
        }
        backend_label = backend

    # Last-100 completion timings — derive p50/p95 first-token + e2e.
    recent = list(_TIMINGS)[-100:]
    durations = [t["duration_ms"] for t in recent]
    completion_tokens = [t["completion_tokens"] for t in recent]
    cache_hits = sum(1 for t in recent if t["cache_reuse_hit"])

    return {
        "backend": backend_label,
        "configured_kind": backend,
        "base_url": probe.get("base_url"),
        "healthy": probe.get("healthy"),
        "declared_models": probe.get("declared_models") or [],
        "resident_models": probe.get("resident_models") or [],
        "probe_error": probe.get("error"),
        "completions_recent": {
            "samples": len(recent),
            "p50_ms": _percentile(durations, 50),
            "p95_ms": _percentile(durations, 95),
            "completion_tokens_p50": _percentile(
                [float(t) for t in completion_tokens], 50,
            ),
            "cache_reuse_hits": cache_hits,
            "cache_reuse_hits_total": _CACHE_REUSE_HITS,
        },
        "swap_events_recent": list(_SWAP_EVENTS)[-20:],
    }


@router.get("/models")
async def list_models(
    user: User = Depends(get_current_user),
) -> List[Dict[str, Any]]:
    """Flat model list, regardless of backend.  Each entry has at
    least ``{id, name}``; resident-only fields (size_vram etc.) added
    where the backend exposes them."""
    state = await get_llm_state(user=user)
    return state.get("declared_models") or []


@router.post("/swap-to/{model_id}")
async def trigger_swap(
    model_id: str,
    user: User = Depends(get_current_user),
) -> Dict[str, Any]:
    """Force llama-swap to load a specific model by issuing a tiny
    completion against it.  Returns timing + final resident-model
    state so the UI can show a "Loading…" spinner that resolves
    when the model is up.

    No-op for Ollama (it already lazy-loads on first request).
    """
    backend = _resolve_active_backend()
    if backend not in {"llama-cpp", "llama_cpp", "llamacpp", "llama.cpp",
                       "llama-swap", "llama_swap", "llamaswap"}:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"swap-to is llama-swap-only; current backend is {backend!r}",
        )

    base = _llamaswap_base_url().rstrip("/")
    started = time.perf_counter()
    try:
        async with httpx.AsyncClient(timeout=180.0) as client:
            r = await client.post(
                base + "/v1/chat/completions",
                json={
                    "model": model_id,
                    "messages": [{"role": "user", "content": "ping"}],
                    "max_tokens": 1,
                    "temperature": 0,
                    "stream": False,
                },
            )
            cold_load_ms = (time.perf_counter() - started) * 1000.0
            if r.status_code >= 400:
                raise HTTPException(
                    status_code=status.HTTP_502_BAD_GATEWAY,
                    detail=f"llama-swap responded {r.status_code}: {r.text[:200]}",
                )
            record_swap_event(
                from_model=None, to_model=model_id,
                cold_load_ms=cold_load_ms,
            )
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"swap probe failed: {exc}",
        ) from exc

    return {
        "model_id": model_id,
        "cold_load_ms": int(cold_load_ms),
        "ok": True,
    }


__all__ = [
    "router",
    "record_completion_timing",
    "record_swap_event",
]
