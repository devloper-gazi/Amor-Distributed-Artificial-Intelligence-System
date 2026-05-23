"""Cycle UI v2.7.2 (D7) — multimodal vision model capability detection.

When the user attaches an image, the resolver branches:
* `has_vision_model=True`  → image bytes forwarded to LLM via
  `ChatMessage.images` (base64-encoded list); inclusion="image_ref".
* `has_vision_model=False` → filename + dimensions banner block;
  inclusion="filename_only".

Detection strategy (single-pass, cached for 60 s):
1. Query Ollama's `/api/tags` to list locally-installed models.
2. Match against the canonical vision-model name whitelist
   (`_VISION_NAME_PATTERNS`) — substring match, case-insensitive.
3. Return True at the first hit, False otherwise.

Network failure: defensive fallback to False (image upload still
works as `filename_only` — the worst case is the user sees the
filename-only banner unnecessarily, which is the v2.7.1 behaviour
anyway).

The whitelist is intentionally conservative: only models known to
support the Ollama multimodal API (`messages[i].images` payload).
Operators who pull a new vision model can extend the whitelist via
`config/settings.py:VISION_MODEL_NAMES_EXTRA` (env-overridable).
"""

from __future__ import annotations

import logging
import time
from typing import Optional

import httpx

from ..config.settings import settings

logger = logging.getLogger(__name__)


# Canonical vision-capable model name substrings (case-insensitive).
# Match list curated from Ollama Library (May 2026 snapshot):
# https://ollama.com/library?c=vision
_VISION_NAME_PATTERNS: tuple[str, ...] = (
    "qwen2-vl",      # Alibaba Qwen2-VL family
    "qwen2.5-vl",    # Qwen 2.5-VL family
    "llava",         # LLaVA + LLaVA-Next + LLaVA-Phi3
    "bakllava",      # Mistral + LLaVA fork
    "phi3-vision",   # Microsoft Phi-3 vision
    "phi-3-vision",  # Same model, alt naming
    "moondream",     # Moondream tiny vision model
    "minicpm-v",     # MiniCPM-V family
    "llama3.2-vision",  # Meta Llama 3.2 vision
    "pixtral",       # Mistral Pixtral
    "internvl",      # InternVL family
    "cogvlm",        # CogVLM
)


# ─── Cache (single instance, 60 s TTL) ───────────────────────────────


_cache: dict[str, object] = {"ts": 0.0, "result": False, "models": []}
_CACHE_TTL_SECONDS = 60.0


def _cache_fresh() -> bool:
    return (time.monotonic() - float(_cache["ts"] or 0.0)) < _CACHE_TTL_SECONDS


def _is_vision_model_name(name: str) -> bool:
    """Pure-function name match.  Exposed so tests can pin specific
    naming conventions without spinning up an Ollama mock."""
    if not name:
        return False
    lowered = name.lower()
    for pattern in _VISION_NAME_PATTERNS:
        if pattern in lowered:
            return True
    # Settings-override allowlist for operator-installed models that
    # the canonical list doesn't yet name.
    extra = getattr(settings, "VISION_MODEL_NAMES_EXTRA", "") or ""
    for token in extra.split(","):
        token = token.strip().lower()
        if token and token in lowered:
            return True
    return False


async def detect_vision_capability(*, force_refresh: bool = False) -> bool:
    """Async — query Ollama tags + cache 60 s.

    Returns:
        True when a vision-capable model is installed locally.
        False on no match OR on transport failure (defensive).
    """
    if not force_refresh and _cache_fresh():
        return bool(_cache["result"])

    ollama_url = getattr(settings, "ollama_url", None) or "http://ollama:11434"
    try:
        async with httpx.AsyncClient(timeout=2.0) as client:
            resp = await client.get(f"{ollama_url}/api/tags")
            resp.raise_for_status()
            payload = resp.json()
    except Exception as exc:
        # Network blip, Ollama down, etc. — fall back to "no vision"
        # so the resolver picks filename_only.  Cache the negative
        # result briefly so the next 5 attachment uploads in this
        # session don't re-hit a dead Ollama.
        logger.info("vision_capability_detect_failed error=%s", exc)
        _cache["ts"] = time.monotonic()
        _cache["result"] = False
        _cache["models"] = []
        return False

    models = payload.get("models") or []
    names = [str(m.get("name") or m.get("model") or "") for m in models]
    has_vision = any(_is_vision_model_name(n) for n in names)
    _cache["ts"] = time.monotonic()
    _cache["result"] = has_vision
    _cache["models"] = names
    logger.info(
        "vision_capability_detected has_vision=%s installed_models=%s",
        has_vision, names,
    )
    return has_vision


def reset_cache_for_test() -> None:
    """Test hook — clear cache so a unit test can re-trigger detection."""
    _cache["ts"] = 0.0
    _cache["result"] = False
    _cache["models"] = []
