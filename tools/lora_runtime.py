"""
Cycle F Sprint 3 — runtime helper for per-request LoRA adapter routing.

Builds the OpenAI-compat body payload llama.cpp PR #10994 expects:

    "lora": [{"id": 0, "scale": 1.0}, ...]

Inputs:
  * `role`  — active agent role (architect / planner / coder / tester / etc.)
  * `enabled` — master gate (settings.code_lora_enabled)
  * `adapters` — {role: adapter_id} mapping from settings
  * `default_scale` — scale applied unless role-specific override exists

Returns either a non-empty list ready for ChatOptions.extra OR None
(meaning "do not attach a `lora` field to the body" — the request
runs on the base model).

This module is stdlib-only so it imports before any settings module.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Iterable, Mapping

logger = logging.getLogger(__name__)


def parse_role_adapter_map(raw: str | Mapping[str, int] | None) -> dict[str, int]:
    """Tolerant parser for the JSON settings string.

    Accepts a JSON string, a plain dict, or None.  Returns a dict
    keyed by lowercased role names; values are integer adapter IDs.
    Malformed entries are skipped with a warning rather than
    raising — a bad JSON edit should not nuke the request path.
    """

    if raw is None:
        return {}
    if isinstance(raw, Mapping):
        return _coerce(raw)
    if isinstance(raw, str):
        raw = raw.strip()
        if not raw:
            return {}
        try:
            parsed = json.loads(raw)
        except (json.JSONDecodeError, ValueError) as exc:
            logger.warning(
                "lora_runtime: code_lora_role_adapters JSON parse failed: %s",
                exc,
            )
            return {}
        if not isinstance(parsed, Mapping):
            return {}
        return _coerce(parsed)
    return {}


def _coerce(mp: Mapping[str, Any]) -> dict[str, int]:
    out: dict[str, int] = {}
    for k, v in mp.items():
        if not isinstance(k, str):
            continue
        try:
            out[k.strip().lower()] = int(v)
        except (TypeError, ValueError):
            logger.warning(
                "lora_runtime: skipping bad adapter mapping %r=%r", k, v,
            )
    return out


def lora_payload_for_role(
    role: str | None,
    *,
    enabled: bool = False,
    adapters: dict[str, int] | None = None,
    default_scale: float = 1.0,
    role_scales: Mapping[str, float] | None = None,
) -> list[dict[str, Any]] | None:
    """Return a `[{"id": int, "scale": float}, ...]` payload for the
    role, or None when LoRA is disabled / role not mapped / no role.

    Multiple roles can stack adapters via `role_scales` overrides;
    today we only attach a single adapter per request (the role's
    own).  Future ensembles can extend this.
    """

    if not enabled:
        return None
    if not role:
        return None
    adapters = adapters or {}
    adapter_id = adapters.get(role.strip().lower())
    if adapter_id is None:
        return None
    role_scales = role_scales or {}
    scale = float(role_scales.get(role.strip().lower(), default_scale))
    return [{"id": int(adapter_id), "scale": scale}]


def disable_all_adapters_payload(adapter_ids: Iterable[int]) -> list[dict[str, Any]]:
    """Build a `[{"id": i, "scale": 0.0}]` list that turns OFF every
    listed adapter — used by `promote.py` to flip away from the
    in-production LoRA before swapping in a new candidate.
    """

    return [{"id": int(i), "scale": 0.0} for i in adapter_ids]


__all__ = [
    "disable_all_adapters_payload",
    "lora_payload_for_role",
    "parse_role_adapter_map",
]
