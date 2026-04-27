"""
Observability — @traced decorator + Langfuse / JSONL fallback.

Wraps any async function with a span emitter. When ``code_langfuse_url``
is configured (Settings) and the ``langfuse`` package is importable,
spans are sent there; otherwise we fall through to a local JSONL trace
file under ``document_processor/code_intelligence/traces/{YYYY-MM-DD}.jsonl``.

The decorator is failure-quiet: an exporter outage MUST NOT poison the
function it wraps. Span context (start, end, exception, attributes,
duration_ms) is captured even when no exporter is reachable.
"""

from __future__ import annotations

import asyncio
import functools
import json
import logging
import time
import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, TypeVar

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# Backend selection — lazy + optional
# ─────────────────────────────────────────────────────────────────────────────


_T = TypeVar("_T")
_TRACE_DIR = Path(__file__).parent / "traces"
_LANGFUSE_CLIENT: Any = None  # populated lazily
_LANGFUSE_TRIED = False


def _trace_path_today() -> Path:
    today = datetime.now(UTC).strftime("%Y-%m-%d")
    _TRACE_DIR.mkdir(parents=True, exist_ok=True)
    return _TRACE_DIR / f"{today}.jsonl"


def _try_init_langfuse() -> Any:
    """
    Best-effort Langfuse init. Returns the client or None. Cached.
    Reads settings at first call so env-var changes after import don't
    require a restart.
    """
    global _LANGFUSE_CLIENT, _LANGFUSE_TRIED
    if _LANGFUSE_TRIED:
        return _LANGFUSE_CLIENT
    _LANGFUSE_TRIED = True

    try:
        from ..config.settings import settings
    except Exception:
        return None

    url = (getattr(settings, "code_langfuse_url", "") or "").strip()
    pk = (getattr(settings, "code_langfuse_public_key", "") or "").strip()
    sk = (getattr(settings, "code_langfuse_secret_key", "") or "").strip()
    if not (url and pk and sk):
        return None

    try:
        # langfuse is optional; absence falls through to JSONL.
        from langfuse import Langfuse  # type: ignore[import-not-found]

        _LANGFUSE_CLIENT = Langfuse(
            host=url,
            public_key=pk,
            secret_key=sk,
        )
        logger.info("observability_langfuse_initialised url=%s", url)
        return _LANGFUSE_CLIENT
    except ImportError:
        logger.info("observability_langfuse_unavailable_falling_back_to_jsonl")
        return None
    except Exception as exc:  # pragma: no cover
        logger.warning("observability_langfuse_init_failed: %s", exc)
        return None


# ─────────────────────────────────────────────────────────────────────────────
# Span emission
# ─────────────────────────────────────────────────────────────────────────────


def _emit_span(span: dict[str, Any]) -> None:
    """Emit one span. Tries Langfuse, then JSONL, never raises."""
    client = _try_init_langfuse()
    if client is not None:
        try:
            # Map to a Langfuse generation/span.
            client.trace(
                id=span.get("trace_id"),
                name=span.get("name"),
                metadata={
                    "role": span.get("role"),
                    "duration_ms": span.get("duration_ms"),
                    "attributes": span.get("attributes", {}),
                    "status": span.get("status"),
                },
            )
            return
        except Exception:  # pragma: no cover
            pass

    # JSONL fallback.
    try:
        with _trace_path_today().open("a", encoding="utf-8") as f:
            f.write(json.dumps(span, default=str) + "\n")
    except Exception:  # pragma: no cover
        # Last-ditch: log only.
        logger.debug("observability_jsonl_write_failed span=%s", span)


# ─────────────────────────────────────────────────────────────────────────────
# Decorator
# ─────────────────────────────────────────────────────────────────────────────


def traced(
    role: str,
    name: str | None = None,
    capture_args: bool = False,
) -> Callable[[Callable[..., Awaitable[_T]]], Callable[..., Awaitable[_T]]]:
    """
    Decorate an async function so every call emits a span.

    Parameters
    ----------
    role           : Logical role tag (e.g. "agent.coder", "sandbox.execute",
                     "registry.pull"). Goes onto the span as ``role``.
    name           : Span name. Defaults to ``func.__qualname__``.
    capture_args   : Include ``args``/``kwargs`` in span attributes
                     (verbose; off by default).

    The wrapper guarantees:
      - The wrapped function's return value flows through unchanged.
      - The wrapped function's exception propagates unchanged AFTER the
        span is closed.
      - Span emission failures are swallowed.
    """
    span_name = name

    def _decorate(
        func: Callable[..., Awaitable[_T]],
    ) -> Callable[..., Awaitable[_T]]:
        resolved_name = span_name or func.__qualname__

        @functools.wraps(func)
        async def _wrapper(*args: Any, **kwargs: Any) -> _T:
            trace_id = uuid.uuid4().hex
            t0 = time.monotonic()
            attrs: dict[str, Any] = {}
            if capture_args:
                # Cap repr length so a multi-MB doc string doesn't bloat.
                attrs["args"] = [repr(a)[:300] for a in args[:5]]
                attrs["kwargs"] = {k: repr(v)[:300] for k, v in list(kwargs.items())[:8]}
            try:
                result = await func(*args, **kwargs)
                duration_ms = int((time.monotonic() - t0) * 1000)
                _emit_span(
                    {
                        "trace_id": trace_id,
                        "name": resolved_name,
                        "role": role,
                        "status": "ok",
                        "duration_ms": duration_ms,
                        "started_at": datetime.now(UTC).isoformat(),
                        "attributes": attrs,
                    }
                )
                return result
            except asyncio.CancelledError:
                duration_ms = int((time.monotonic() - t0) * 1000)
                _emit_span(
                    {
                        "trace_id": trace_id,
                        "name": resolved_name,
                        "role": role,
                        "status": "cancelled",
                        "duration_ms": duration_ms,
                        "started_at": datetime.now(UTC).isoformat(),
                        "attributes": attrs,
                    }
                )
                raise
            except Exception as exc:
                duration_ms = int((time.monotonic() - t0) * 1000)
                _emit_span(
                    {
                        "trace_id": trace_id,
                        "name": resolved_name,
                        "role": role,
                        "status": "error",
                        "duration_ms": duration_ms,
                        "started_at": datetime.now(UTC).isoformat(),
                        "error": f"{exc.__class__.__name__}: {exc}"[:500],
                        "attributes": attrs,
                    }
                )
                raise

        return _wrapper

    return _decorate


# ─────────────────────────────────────────────────────────────────────────────
# Convenience: emit a free-standing event (not tied to a function call)
# ─────────────────────────────────────────────────────────────────────────────


def emit_event(role: str, name: str, **attributes: Any) -> None:
    """Emit a standalone span (zero duration). Useful for milestones."""
    _emit_span(
        {
            "trace_id": uuid.uuid4().hex,
            "name": name,
            "role": role,
            "status": "event",
            "duration_ms": 0,
            "started_at": datetime.now(UTC).isoformat(),
            "attributes": attributes,
        }
    )
