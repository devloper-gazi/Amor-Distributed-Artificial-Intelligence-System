"""
Code Intelligence diagnostics — Phase 17 Commit R.

Pure-data collectors that the ``GET /api/code/diagnostics`` route
calls.  Every collector:

* Returns a JSON-serialisable ``dict``.
* Never raises — degrades gracefully to ``{"error": "..."}``.
* Is short-circuited by a 30-second TTL cache so the UI can poll
  cheaply.

The user's complaint that triggered Phase 17 was "Mevcut sistem
düzgün çalışmıyor".  Without this endpoint they cannot see *why*
a session looks weak or which subsystem is in a degraded state;
diagnostics ships first so every later commit (planner spec,
AlphaCodium reorder, diff-mode debugger, browser sandbox, …) has
visible signal.

License: MIT.
"""

from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any


logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────
# TTL cache — keeps the route cheap when the UI polls every 5 s
# ─────────────────────────────────────────────────────────────────────


@dataclass
class _CachedSection:
    payload: dict
    fetched_at: float
    ttl_s: float


_CACHE: dict[str, _CachedSection] = {}


def _cached(key: str, ttl_s: float, fetcher) -> dict:
    now = time.monotonic()
    hit = _CACHE.get(key)
    if hit is not None and (now - hit.fetched_at) < hit.ttl_s:
        return hit.payload
    try:
        payload = fetcher()
    except Exception as exc:  # pragma: no cover
        logger.debug("diagnostics %s collector failed: %s", key, exc)
        payload = {"error": f"{type(exc).__name__}: {exc}"}
    _CACHE[key] = _CachedSection(
        payload=payload, fetched_at=now, ttl_s=float(ttl_s),
    )
    return payload


def reset_cache() -> None:
    """Test helper — wipe the TTL cache."""
    _CACHE.clear()


# ─────────────────────────────────────────────────────────────────────
# Cold-start telemetry — sandbox runs append; diagnostics reads
# ─────────────────────────────────────────────────────────────────────


_SANDBOX_TIMINGS: list[float] = []
_SANDBOX_TIMINGS_MAX = 200


def record_sandbox_run_ms(elapsed_ms: float) -> None:
    """Sandbox calls this after every successful run.  Sliding window
    of the last 200 timings powers the p50/p95 in the diagnostics
    payload."""
    if elapsed_ms <= 0:
        return
    _SANDBOX_TIMINGS.append(float(elapsed_ms))
    if len(_SANDBOX_TIMINGS) > _SANDBOX_TIMINGS_MAX:
        # Drop the oldest in O(n) — fine for a 200-entry list.
        del _SANDBOX_TIMINGS[: len(_SANDBOX_TIMINGS) - _SANDBOX_TIMINGS_MAX]


def reset_sandbox_timings() -> None:
    """Test helper."""
    _SANDBOX_TIMINGS.clear()


def _percentile(values: list[float], pct: float) -> float | None:
    if not values:
        return None
    s = sorted(values)
    if pct <= 0:
        return s[0]
    if pct >= 100:
        return s[-1]
    # Nearest-rank, simple + monotonic.
    rank = int(round((pct / 100.0) * (len(s) - 1)))
    rank = max(0, min(len(s) - 1, rank))
    return round(s[rank], 1)


# ─────────────────────────────────────────────────────────────────────
# Recent-failure ring buffer — sandbox + engine append on every
# error path; diagnostics reads.
# ─────────────────────────────────────────────────────────────────────


@dataclass
class _Failure:
    ts: str
    where: str
    detail: str
    payload: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "ts": self.ts, "where": self.where, "detail": self.detail,
            "payload": dict(self.payload or {}),
        }


_FAILURES: list[_Failure] = []
_FAILURES_MAX = 30


def record_failure(where: str, detail: str, **payload) -> None:
    _FAILURES.append(_Failure(
        ts=_now_iso(), where=where[:80],
        detail=str(detail)[:400], payload=dict(payload),
    ))
    if len(_FAILURES) > _FAILURES_MAX:
        del _FAILURES[: len(_FAILURES) - _FAILURES_MAX]


def reset_failures() -> None:
    _FAILURES.clear()


# ─────────────────────────────────────────────────────────────────────
# Section collectors
# ─────────────────────────────────────────────────────────────────────


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def collect_backend() -> dict:
    """Pluggable LLM backend status."""
    try:
        from local_ai.llm_backend import get_backend  # noqa: PLC0415
        backend = get_backend()
        return {
            "kind": getattr(backend, "name", "unknown"),
            "url": getattr(backend, "base_url", ""),
            "class": type(backend).__name__,
        }
    except Exception as exc:  # pragma: no cover
        return {"error": f"{type(exc).__name__}: {exc}"}


async def collect_backend_health() -> dict:
    """Async health probe — runs once per 30s."""
    try:
        from local_ai.llm_backend import get_backend  # noqa: PLC0415
        backend = get_backend()
        ok = await backend.health_check()
        return {"healthy": bool(ok)}
    except Exception as exc:
        return {"healthy": False,
                "error": f"{type(exc).__name__}: {exc}"}


async def collect_models(*, base_url: str | None = None) -> dict:
    """Currently-installed Ollama tags + auto-derived role assignment."""
    try:
        from ..config.settings import settings  # noqa: PLC0415
        from .model_registry import CodeModelRegistry  # noqa: PLC0415
        url = (
            base_url
            or getattr(settings, "code_ollama_base_url", None)
            or os.environ.get("OLLAMA_BASE_URL", "http://localhost:11434")
        )
        reg = CodeModelRegistry(url)
        installed = await reg.probe()
        # v17 PR #1 — use architect/editor (the engine fires those at
        # phase boundaries).  Keeps the diagnostics role table in
        # sync with what the live pipeline actually routes.
        roles = ["architect", "editor", "tester", "debugger", "critic"]
        chosen = reg.select_models_for_session(
            roles, effort=getattr(settings, "code_default_effort", "medium"),
            spread=True,
        )
        role_assignment = {r: spec.ollama_tag for r, spec in chosen.items()}
        # Aggregate VRAM estimate for distinct tags.
        distinct = {spec.ollama_tag: spec.vram_gb for spec in chosen.values()}
        vram_estimate = round(sum(distinct.values()), 1)
        return {
            "installed": list(installed),
            "role_assignment": role_assignment,
            "distinct_count": len(set(role_assignment.values())),
            "vram_usage_estimate_gb": vram_estimate,
        }
    except Exception as exc:  # pragma: no cover
        return {"error": f"{type(exc).__name__}: {exc}"}


async def collect_sandbox(probe: bool = True) -> dict:
    """Sandbox health + cold-start telemetry."""
    out: dict[str, Any] = {
        "workdir_root": None,
        "named_volume": "amor-sandbox-shared",
        "cold_start_p50_ms": _percentile(_SANDBOX_TIMINGS, 50),
        "cold_start_p95_ms": _percentile(_SANDBOX_TIMINGS, 95),
        "samples": len(_SANDBOX_TIMINGS),
        "recent_failures": [
            f.to_dict() for f in _FAILURES if f.where.startswith("sandbox")
        ][-10:],
    }
    try:
        from .sandbox import ExecutionSandbox  # noqa: PLC0415
        sb = ExecutionSandbox()
        out["workdir_root"] = sb._workdir_root  # noqa: SLF001
        if probe:
            t0 = time.monotonic()
            ok = await sb.docker_available(force_refresh=False)
            elapsed = (time.monotonic() - t0) * 1000.0
            out["docker_available"] = bool(ok)
            out["probe"] = {"ok": bool(ok), "elapsed_ms": round(elapsed, 1)}
        else:
            out["docker_available"] = None
            out["probe"] = None
    except Exception as exc:  # pragma: no cover
        out["error"] = f"{type(exc).__name__}: {exc}"
    return out


def collect_rag() -> dict:
    try:
        from ..config.settings import settings  # noqa: PLC0415
        return {
            "embedder": getattr(
                settings, "rag_embedding_model",
                "nomic-ai/nomic-embed-text-v1.5",
            ),
            "hybrid_enabled": bool(
                getattr(settings, "rag_hybrid_search_enabled", True),
            ),
            "rrf_k": int(getattr(settings, "rag_rrf_k", 60)),
            "reranker_enabled": bool(
                getattr(settings, "rag_reranker_enabled", False),
            ),
            "reranker_top_k": int(
                getattr(settings, "rag_reranker_top_k", 20),
            ),
            "chunking_strategy": getattr(
                settings, "rag_chunking_strategy", "naive",
            ),
            "per_model_table": bool(
                getattr(settings, "rag_per_model_table", True),
            ),
        }
    except Exception as exc:  # pragma: no cover
        return {"error": f"{type(exc).__name__}: {exc}"}


def collect_ledger() -> dict:
    """Phase 15 immutable ledger integrity status."""
    try:
        from ..config.settings import settings  # noqa: PLC0415
        from ..sentinel.evolution.governance import (  # noqa: PLC0415
            LedgerStore,
        )
        root = getattr(
            settings, "sentinel_evolution_root", "data/sentinel/evolution",
        )
        store = LedgerStore(root)
        entries = store.entries()
        return {
            "intact": bool(store.verify()),
            "entries": len(entries),
            "tail_hash": (store.tail_hash or "")[:12],
            "root": str(root),
        }
    except Exception as exc:  # pragma: no cover
        return {"error": f"{type(exc).__name__}: {exc}"}


def collect_phase16_facade() -> dict:
    try:
        from ..config.settings import settings  # noqa: PLC0415
        return {
            "openai_compat_enabled": bool(
                getattr(settings, "openai_compat_enabled", True),
            ),
            "mcp_server_enabled": bool(
                getattr(settings, "enable_mcp_server", False),
            ),
            "llm_backend": getattr(settings, "llm_backend", "ollama"),
        }
    except Exception as exc:  # pragma: no cover
        return {"error": f"{type(exc).__name__}: {exc}"}


def collect_recent_sessions(sessions_map: dict) -> list[dict]:
    """Last 5 sessions snapshot — caller passes the in-memory map.

    We deliberately don't import _sessions here because the route
    layer holds it; passing it in keeps this module testable in
    isolation."""
    out: list[dict] = []
    if not isinstance(sessions_map, dict):
        return out
    items = list(sessions_map.values())
    items.sort(key=lambda s: s.get("started_at_ts") or 0, reverse=True)
    for sess in items[:5]:
        phases = sess.get("phases") or []
        phases_failed = [
            p.get("name") for p in phases if p.get("status") == "failed"
        ]
        out.append({
            "sid": sess.get("session_id") or "",
            "status": sess.get("status") or "unknown",
            "current_phase": sess.get("current_phase"),
            "models_used": dict(sess.get("models_used") or {}),
            "phases_failed": phases_failed,
            "started_at": sess.get("started_at"),
        })
    return out


# ─────────────────────────────────────────────────────────────────────
# Top-level assembler
# ─────────────────────────────────────────────────────────────────────


async def build_diagnostics(
    sessions_map: dict | None = None,
    *,
    probe_sandbox: bool = True,
) -> dict:
    """Assemble the full diagnostics payload.

    ``sessions_map`` is the live in-memory session cache from the
    code-intelligence route (passed in to keep this module
    side-effect-free).
    """
    return {
        "ts": _now_iso(),
        "backend": _cached("backend", 30.0, collect_backend),
        "backend_health": await collect_backend_health(),
        "models": await collect_models(),
        "sandbox": await collect_sandbox(probe=probe_sandbox),
        "rag": _cached("rag", 30.0, collect_rag),
        "ledger": _cached("ledger", 30.0, collect_ledger),
        "phase16_facade": _cached("phase16_facade", 30.0, collect_phase16_facade),
        "recent_sessions": collect_recent_sessions(sessions_map or {}),
        "recent_failures": [f.to_dict() for f in _FAILURES][-10:],
    }


__all__ = [
    "build_diagnostics",
    "record_sandbox_run_ms",
    "record_failure",
    "reset_cache",
    "reset_sandbox_timings",
    "reset_failures",
    "collect_backend",
    "collect_backend_health",
    "collect_models",
    "collect_sandbox",
    "collect_rag",
    "collect_ledger",
    "collect_phase16_facade",
    "collect_recent_sessions",
]
