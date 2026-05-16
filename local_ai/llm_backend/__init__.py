"""
``local_ai.llm_backend`` — Phase 16 pluggable LLM inference layer.

Public surface:

* ABC + dataclasses (``LLMBackend``, ``Embedder``, ``ChatMessage``,
  ``ChatResponse``, ``ChatChoice``, ``ChatUsage``, ``ChatOptions``)
* Concrete backends (``OllamaBackend``, ``StubBackend`` today;
  ``LlamaSwapBackend``/``LlamaCppBackend``/``OpenAICompatibleBackend``
  in Commit B).
* Factory + singleton (``make_backend``, ``get_backend``).
* Test helpers (``_reset_backend_cache``, ``_set_backend``).

The default backend kind is resolved in this order:

1. Explicit ``kind`` arg to ``get_backend(kind=...)``
2. ``settings.llm_backend`` (`document_processor/config/settings.py`)
3. ``$AMOR_LLM_BACKEND`` env var
4. ``"ollama"`` (last-resort default — preserves today's behaviour)

License: MIT.
"""

from __future__ import annotations

import os
import threading
from typing import Optional

from .base import (
    ChatChoice,
    ChatMessage,
    ChatOptions,
    ChatResponse,
    ChatUsage,
    Embedder,
    LLMBackend,
    normalize_messages,
)
from .ollama import OllamaBackend
from .stub import StubBackend


# ─── singleton cache ───────────────────────────────────────────────


_BACKEND_LOCK = threading.Lock()
_BACKEND_CACHE: dict[str, LLMBackend] = {}


def _resolve_kind() -> str:
    """Pick the backend kind from settings / env / default."""
    try:
        from document_processor.config.settings import (  # noqa: PLC0415
            settings as _settings,
        )
        kind = (getattr(_settings, "llm_backend", None) or "").strip().lower()
        if kind:
            return kind
    except Exception:
        pass
    # v18.1 bug-pattern fix — os.environ.get(KEY, default) returns ""
    # when KEY exists but is empty (the docker-compose
    # ``AMOR_LLM_BACKEND=`` case).  Falsy-skip so empty falls through
    # to the explicit default.
    env_kind = (os.environ.get("AMOR_LLM_BACKEND") or "").strip().lower()
    return env_kind if env_kind else "ollama"


def _resolve_url() -> str:
    """Pick the backend URL — settings first, then OLLAMA_BASE_URL,
    then localhost default.  Used by every backend that takes one."""
    try:
        from document_processor.config.settings import (  # noqa: PLC0415
            settings as _settings,
        )
        url = (getattr(_settings, "llm_backend_url", None) or "").strip()
        if url:
            return url
    except Exception:
        pass
    # v18.1 bug-pattern fix — same falsy-skip as _resolve_kind so an
    # empty AMOR_LLM_BACKEND_URL falls through to OLLAMA_BASE_URL
    # falls through to the localhost default (instead of returning
    # the literal "" from a defined-but-empty env var).
    for key in ("AMOR_LLM_BACKEND_URL", "OLLAMA_BASE_URL"):
        v = (os.environ.get(key) or "").strip()
        if v:
            return v
    return "http://localhost:11434"


def make_backend(kind: str, *, url: Optional[str] = None) -> LLMBackend:
    """Construct a backend by kind.  ``url`` overrides settings/env.

    Raises ``ValueError`` on unknown kind.  Lazy-imports the heavier
    backends so a bare ``import local_ai.llm_backend`` is cheap.
    """
    base_url = url if url is not None else _resolve_url()
    k = kind.strip().lower()
    if k == "ollama":
        return OllamaBackend(base_url=base_url)
    if k == "stub":
        return StubBackend()
    if k in {"llama-swap", "llama_swap", "llamaswap"}:
        from .llama_swap import LlamaSwapBackend  # noqa: PLC0415
        return LlamaSwapBackend(base_url=base_url)
    if k in {"llama-cpp", "llama_cpp", "llamacpp", "llama.cpp"}:
        from .llama_cpp import LlamaCppBackend  # noqa: PLC0415
        return LlamaCppBackend(base_url=base_url)
    if k in {"bitnet-cpu", "bitnet_cpu", "bitnet", "bitnetcpu"}:
        # Cycle H Phase A.1 — BitNet b1.58 2B4T ternary CPU planner.
        # `url=None` lets the backend pick its own default port (8081)
        # so the operator running BOTH llama-cpp (:8080) AND bitnet-cpu
        # side-by-side doesn't get a bind conflict.
        from .bitnet_cpu import BitNetCpuBackend  # noqa: PLC0415
        return BitNetCpuBackend(base_url=url)
    if k in {"openai-compat", "openai_compat", "openai-compatible"}:
        from .openai_compat import OpenAICompatibleBackend  # noqa: PLC0415
        return OpenAICompatibleBackend(base_url=base_url)
    raise ValueError(f"unknown llm_backend: {kind!r}")


def get_backend(kind: Optional[str] = None) -> LLMBackend:
    """Singleton accessor — returns the same instance per kind.

    ``kind=None`` resolves via ``_resolve_kind()``; pass an explicit
    kind to bypass the resolver (used in tests + multi-backend hosts).
    """
    resolved = (kind or _resolve_kind()).lower()
    with _BACKEND_LOCK:
        if resolved not in _BACKEND_CACHE:
            _BACKEND_CACHE[resolved] = make_backend(resolved)
        return _BACKEND_CACHE[resolved]


# ─── test helpers ───────────────────────────────────────────────────


def _reset_backend_cache() -> None:
    """Drop every cached backend.  Test-only — production code should
    not call this (singletons exist for a reason)."""
    with _BACKEND_LOCK:
        _BACKEND_CACHE.clear()


def _set_backend(kind: str, backend: LLMBackend) -> None:
    """Inject a backend instance into the singleton cache.  Used to
    swap in a ``StubBackend`` for tests without touching env vars."""
    with _BACKEND_LOCK:
        _BACKEND_CACHE[kind.strip().lower()] = backend


__all__ = [
    # contracts
    "LLMBackend",
    "Embedder",
    "ChatMessage",
    "ChatChoice",
    "ChatUsage",
    "ChatResponse",
    "ChatOptions",
    "normalize_messages",
    # backends
    "OllamaBackend",
    "StubBackend",
    # factory + singleton
    "make_backend",
    "get_backend",
    # test helpers
    "_reset_backend_cache",
    "_set_backend",
]
