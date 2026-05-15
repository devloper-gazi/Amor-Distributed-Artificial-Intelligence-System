"""
Cycle C Sprint 4 Day 2 — repo symbol discovery API.

Backs the @-mention picker in ``UnifiedComposer``:

    GET /api/repo/symbols?q=foo&limit=20

The frontend types ``@`` then narrows on each subsequent keystroke
(debounced 150 ms) — the response is a flat ``[{name, kind, path,
line, parent, scope}]`` list.

Why a dedicated thin route
--------------------------
Sprint 3's ``RepoMap`` is the canonical symbol index.  This route is
deliberately tiny: it singletons one ``RepoMap`` per process, scans
on first hit (mtime-keyed cache so subsequent hits are <2 ms), and
delegates the LIKE-search to SQLite.  No caching layer here — the
repomap cache already does it.

Auth: every endpoint requires ``get_current_user``.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status

from ..auth.dependencies import get_current_user
from ..auth.models import User
from ..i18n import get_locale, localized_http_exception

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/repo", tags=["repo"])


# ─── singleton repo-map ────────────────────────────────────────────


_REPOMAP_LOCK = threading.Lock()
_REPOMAP_INSTANCE: Any = None
_REPOMAP_LAST_SCAN_TS: float = 0.0
# How long to cache "scan was just done" before we bother calling
# ``.scan()`` again.  ``RepoMap.scan()`` is itself fast on a warm
# cache (~50 ms for 50K LOC) — this is just to avoid that 50 ms hit
# on every keystroke.
_REPOMAP_SCAN_TTL_S = 30.0


def _resolve_repo_root() -> Path:
    return Path(os.environ.get("AMOR_REPOMAP_ROOT", "/app"))


def _get_repo_map() -> Any:
    """Lazily import RepoMap (heavy stdlib touch on first call) and
    return a process-wide singleton.  Re-scan if last scan is older
    than the TTL — never on the hot path of a single request.
    """
    global _REPOMAP_INSTANCE, _REPOMAP_LAST_SCAN_TS  # noqa: PLW0603
    from ..services.repo_map import RepoMap  # noqa: PLC0415

    with _REPOMAP_LOCK:
        if _REPOMAP_INSTANCE is None:
            _REPOMAP_INSTANCE = RepoMap(repo_root=_resolve_repo_root())
        now = time.monotonic()
        if now - _REPOMAP_LAST_SCAN_TS > _REPOMAP_SCAN_TTL_S:
            try:
                _REPOMAP_INSTANCE.scan(force=False)
                _REPOMAP_LAST_SCAN_TS = now
            except Exception as exc:  # noqa: BLE001
                logger.warning("repo_map scan failed: %s", exc)
        return _REPOMAP_INSTANCE


# ─── routes ────────────────────────────────────────────────────────


@router.get("/symbols")
def get_symbols(
    q: str = Query("", min_length=0, max_length=128, description="substring"),
    limit: int = Query(20, ge=1, le=100),
    kind: Optional[str] = Query(
        None,
        description="filter by tag kind (def | class | method | const | interface | type | enum)",
    ),
    _user: User = Depends(get_current_user),
    locale: str = Depends(get_locale),
) -> Dict[str, Any]:
    """Substring search across symbols indexed in the live RepoMap.

    Empty ``q`` returns the most recently-indexed symbols (useful for
    bootstrapping the picker on first keystroke).
    """
    rmap = _get_repo_map()
    q_clean = (q or "").strip()
    try:
        if q_clean:
            tags = rmap.search(q_clean, limit=limit)
        else:
            # Cheap "first-page" feed — first ``limit`` symbols by
            # rel_path order.  Avoids forcing the client to type a
            # letter just to see anything.
            tags = []
            for tag in rmap.all_tags():
                tags.append(tag)
                if len(tags) >= limit:
                    break
        if kind:
            tags = [t for t in tags if t.kind == kind]
    except Exception as exc:  # noqa: BLE001
        logger.warning("repo_map search failed: %s", exc)
        raise localized_http_exception(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            key="repo.index_unavailable",
            locale=locale,
        ) from exc

    items: List[Dict[str, Any]] = []
    for t in tags:
        items.append(
            {
                "name": t.name,
                "kind": t.kind,
                "path": t.rel_path,
                "line": int(t.line),
                "end_line": int(t.end_line) if t.end_line else None,
                "parent": t.parent,
                "scope": t.scope_text,
                "label": f"@[{t.name}]({t.rel_path}:{t.line})",
            }
        )
    return {"q": q_clean, "limit": limit, "kind": kind, "count": len(items), "items": items}


@router.get("/stats")
def get_stats(
    _user: User = Depends(get_current_user),
    locale: str = Depends(get_locale),
) -> Dict[str, Any]:
    """Quick health snapshot for ``/admin/repo`` UI / diagnostics."""
    rmap = _get_repo_map()
    try:
        stats = rmap.stats()
    except Exception as exc:  # noqa: BLE001
        logger.warning("repo_map stats failed: %s", exc)
        raise localized_http_exception(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            key="repo.index_unavailable",
            locale=locale,
        ) from exc
    return {
        "repo_root": str(_resolve_repo_root()),
        "last_scan_ts_monotonic": _REPOMAP_LAST_SCAN_TS,
        "scan_ttl_s": _REPOMAP_SCAN_TTL_S,
        **stats,
    }
