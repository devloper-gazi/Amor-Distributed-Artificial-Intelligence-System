"""
CodeModelRegistry — discovers, ranks, and manages local Ollama models
for code-intelligence tasks. Zero external API calls.

The registry has three jobs:

1. Maintain a curated catalogue of code-capable open-source models with
   the metadata needed to score them (params, VRAM, benchmarks, license,
   context, strengths, tier).
2. Probe the local Ollama daemon to discover what's already installed,
   caching the result in Redis so repeated probes don't hammer Ollama.
3. Pick the best model for a given agent role + effort tier and pull
   it on-demand if it's missing — streaming progress events to the
   caller so the UI can render a download banner.

Design notes
------------
* Strict no-network-to-LLM-vendor: this module only talks to Ollama
  on localhost (or the Docker network). It never reaches anthropic.com,
  openai.com, etc. — see the catalogue: every entry is open-source.
* The class is **stateless across requests** at the data level — all
  durable state (the probe result) lives in Redis. A new instance
  reads the cached probe and is immediately useful.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

import httpx

from ..infrastructure.cache import cache_manager

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# Model catalogue
# ─────────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class ModelSpec:
    """Static metadata for a single code-capable Ollama model."""

    ollama_tag: str  # Exact tag for `ollama pull` / API calls
    display_name: str
    params_b: float  # Parameter count in billions
    vram_gb: float  # Approximate VRAM at Q4_K_M quantisation
    swebench_pct: float  # SWE-bench Lite verified % (0 if unknown)
    humaneval_pct: float  # HumanEval pass@1 %
    context_k: int  # Context window in thousands of tokens
    strengths: list[str]  # e.g. ["python", "debugging"]
    tier: str  # "flagship" | "balanced" | "lightweight"
    license: str

    def to_dict(self) -> dict[str, Any]:
        """JSON-friendly view for the /api/code/models endpoint."""
        return {
            "tag": self.ollama_tag,
            "display_name": self.display_name,
            "params_b": self.params_b,
            "vram_gb": self.vram_gb,
            "swebench_pct": self.swebench_pct,
            "humaneval_pct": self.humaneval_pct,
            "context_k": self.context_k,
            "strengths": list(self.strengths),
            "tier": self.tier,
            "license": self.license,
        }


CODE_MODEL_CATALOGUE: list[ModelSpec] = [
    # ── Flagship tier (≥ 16 GB VRAM) ────────────────────────────────────────
    ModelSpec(
        ollama_tag="devstral:24b",
        display_name="Devstral 24B",
        params_b=24,
        vram_gb=16,
        swebench_pct=68.0,
        humaneval_pct=88.0,
        context_k=128,
        strengths=["multi-file editing", "debugging", "agentic loops", "refactoring"],
        tier="flagship",
        license="Apache-2.0",
    ),
    ModelSpec(
        ollama_tag="qwen2.5-coder:32b",
        display_name="Qwen2.5-Coder 32B",
        params_b=32,
        vram_gb=22,
        swebench_pct=52.1,
        humaneval_pct=92.9,
        context_k=128,
        strengths=["code generation", "python", "typescript", "explanation"],
        tier="flagship",
        license="Apache-2.0",
    ),
    ModelSpec(
        ollama_tag="deepseek-coder-v2:16b",
        display_name="DeepSeek-Coder-V2 16B",
        params_b=16,
        vram_gb=12,
        swebench_pct=43.0,
        humaneval_pct=90.2,
        context_k=128,
        strengths=["code generation", "math", "algorithms"],
        tier="flagship",
        license="DeepSeek",
    ),
    ModelSpec(
        ollama_tag="codellama:34b",
        display_name="CodeLlama 34B",
        params_b=34,
        vram_gb=24,
        swebench_pct=22.0,
        humaneval_pct=53.7,
        context_k=100,
        strengths=["c++", "java", "completion"],
        tier="flagship",
        license="Llama-2",
    ),
    # ── Balanced tier (8–15 GB VRAM) ────────────────────────────────────────
    ModelSpec(
        ollama_tag="qwen2.5-coder:7b",
        display_name="Qwen2.5-Coder 7B",
        params_b=7,
        vram_gb=6,
        swebench_pct=33.0,
        humaneval_pct=88.4,
        context_k=128,
        strengths=["code generation", "python", "fast inference"],
        tier="balanced",
        license="Apache-2.0",
    ),
    ModelSpec(
        ollama_tag="deepseek-coder:6.7b",
        display_name="DeepSeek-Coder 6.7B",
        params_b=6.7,
        vram_gb=5,
        swebench_pct=0,
        humaneval_pct=73.8,
        context_k=16,
        strengths=["python", "completion", "fast"],
        tier="balanced",
        license="Apache-2.0",
    ),
    ModelSpec(
        ollama_tag="starcoder2:15b",
        display_name="StarCoder2 15B",
        params_b=15,
        vram_gb=10,
        swebench_pct=0,
        humaneval_pct=46.3,
        context_k=16,
        strengths=["infilling", "multi-language", "c++"],
        tier="balanced",
        license="BigCode-OpenRAIL",
    ),
    ModelSpec(
        ollama_tag="granite-code:20b",
        display_name="Granite Code 20B",
        params_b=20,
        vram_gb=14,
        swebench_pct=0,
        humaneval_pct=60.8,
        context_k=128,
        strengths=["enterprise", "java", "go", "review"],
        tier="balanced",
        license="Apache-2.0",
    ),
    # ── Lightweight tier (< 8 GB VRAM or CPU) ───────────────────────────────
    ModelSpec(
        ollama_tag="qwen2.5-coder:3b",
        display_name="Qwen2.5-Coder 3B",
        params_b=3,
        vram_gb=3,
        swebench_pct=0,
        humaneval_pct=75.1,
        context_k=32,
        strengths=["fast generation", "code completion"],
        tier="lightweight",
        license="Apache-2.0",
    ),
    ModelSpec(
        ollama_tag="deepseek-coder:1.3b",
        display_name="DeepSeek-Coder 1.3B",
        params_b=1.3,
        vram_gb=1.5,
        swebench_pct=0,
        humaneval_pct=65.2,
        context_k=16,
        strengths=["ultra-fast", "completion", "triage"],
        tier="lightweight",
        license="Apache-2.0",
    ),
    ModelSpec(
        ollama_tag="starcoder2:3b",
        display_name="StarCoder2 3B",
        params_b=3,
        vram_gb=2.5,
        swebench_pct=0,
        humaneval_pct=31.7,
        context_k=16,
        strengths=["fill-in-the-middle", "infilling"],
        tier="lightweight",
        license="BigCode-OpenRAIL",
    ),
    # General-purpose fallback — already in the system as the default
    ModelSpec(
        ollama_tag="qwen2.5:7b",
        display_name="Qwen2.5 7B (general)",
        params_b=7,
        vram_gb=6,
        swebench_pct=0,
        humaneval_pct=55.0,
        context_k=128,
        strengths=["general", "explanation", "planning"],
        tier="balanced",
        license="Apache-2.0",
    ),
]


# Agent role → strengths the model should ideally have. Used by the
# scorer in select_model().
ROLE_STRENGTH_MAP: dict[str, list[str]] = {
    "planner": ["planning", "agentic loops", "explanation", "multi-file editing"],
    "coder": ["code generation", "python", "typescript", "fast inference"],
    "tester": ["code generation", "python", "debugging"],
    "debugger": ["debugging", "multi-file editing", "agentic loops"],
    "critic": ["review", "explanation", "agentic loops"],
    "triage": ["fast generation", "explanation"],
}


# Effort tier → ordered preference of model tiers. The scorer rewards
# matches near the front of the list.
_TIER_PREFERENCE: dict[str, list[str]] = {
    "basic": ["lightweight", "balanced", "flagship"],
    "medium": ["balanced", "lightweight", "flagship"],
    "deep": ["balanced", "flagship", "lightweight"],
    "expert": ["flagship", "balanced", "lightweight"],
    "ultra": ["flagship", "balanced", "lightweight"],
}


ProgressCallback = Callable[[int, int, str], Awaitable[None]]


# ─────────────────────────────────────────────────────────────────────────────
# Registry
# ─────────────────────────────────────────────────────────────────────────────


class CodeModelRegistry:
    """
    Discovers available Ollama models, selects the best for each agent
    role, and optionally pulls missing ones.

    Instances are cheap to create — all durable state lives in Redis.
    """

    _PROBE_CACHE_KEY = "amor:code:model_registry:probe"
    _PROBE_TTL_SECONDS = 300  # 5 minutes

    def __init__(self, ollama_base_url: str):
        self._base_url = ollama_base_url.rstrip("/")
        self._available: list[str] = []
        self._probed = False

    # ── Discovery ──────────────────────────────────────────────────────────

    async def probe(self, force: bool = False) -> list[str]:
        """
        Query Ollama's /api/tags. Result is cached in Redis for 5 min.

        Returns the list of installed Ollama tags (e.g. ["qwen2.5:7b",
        "qwen2.5-coder:7b"]). Falls back to an empty list on any error
        — the registry stays usable but `select_model` will pick a
        candidate that needs to be pulled.
        """
        if not force:
            cached = await cache_manager.get_json(self._PROBE_CACHE_KEY)
            if isinstance(cached, list):
                self._available = cached
                self._probed = True
                return self._available

        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.get(f"{self._base_url}/api/tags")
                resp.raise_for_status()
                data = resp.json()
            self._available = [m["name"] for m in data.get("models", [])]
            await cache_manager.set_json(
                self._PROBE_CACHE_KEY,
                self._available,
                ttl=self._PROBE_TTL_SECONDS,
            )
            self._probed = True
            logger.info(
                "code_registry_probed ollama_models=%d",
                len(self._available),
            )
        except Exception as exc:
            logger.warning("code_registry_probe_failed: %s", exc)
            self._available = []
        return self._available

    @property
    def available(self) -> list[str]:
        """Last-known installed tags. Call `probe()` first to populate."""
        return list(self._available)

    def _tag_installed(self, tag: str) -> bool:
        """
        True if either the exact tag is installed, OR a tag with the same
        base name is installed (Ollama sometimes appends `-instruct` or
        `-chat` suffixes inconsistently).
        """
        normalized = tag.lower()
        base = normalized.split(":", 1)[0] + ":"
        for av in self._available:
            av_norm = av.lower()
            if av_norm == normalized or av_norm.startswith(base):
                return True
        return False

    # ── Pulling ────────────────────────────────────────────────────────────

    async def pull_model(
        self,
        tag: str,
        on_progress: ProgressCallback | None = None,
    ) -> bool:
        """
        Pull a model from Ollama, streaming progress.

        `on_progress(bytes_done, bytes_total, status)` is called for every
        chunk. Returns True on success, False on any failure.
        """
        logger.info("code_registry_pulling tag=%s", tag)
        try:
            async with (
                httpx.AsyncClient(timeout=None) as client,
                client.stream(
                    "POST",
                    f"{self._base_url}/api/pull",
                    json={"name": tag, "stream": True},
                ) as resp,
            ):
                resp.raise_for_status()
                async for line in resp.aiter_lines():
                    if not line.strip():
                        continue
                    try:
                        chunk = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    status = chunk.get("status", "")
                    completed = int(chunk.get("completed", 0) or 0)
                    total = int(chunk.get("total", 0) or 0)
                    if on_progress:
                        try:
                            await on_progress(completed, total, status)
                        except Exception:  # pragma: no cover
                            pass
                    if chunk.get("error"):
                        logger.error(
                            "code_registry_pull_error tag=%s error=%s",
                            tag,
                            chunk["error"],
                        )
                        return False
            # Invalidate probe cache so the next select_model() sees the
            # new tag.
            await self.probe(force=True)
            logger.info("code_registry_pull_complete tag=%s", tag)
            return True
        except Exception as exc:
            logger.error("code_registry_pull_failed tag=%s error=%s", tag, exc)
            return False

    # ── Selection ──────────────────────────────────────────────────────────

    def select_model(
        self,
        role: str,
        effort: str = "medium",
        require_tier: str | None = None,
    ) -> tuple[ModelSpec, bool]:
        """
        Pick the best available model for ``role`` given an effort tier.

        Returns ``(ModelSpec, is_already_installed)``. If nothing is
        installed yet, returns the best candidate to auto-pull.

        Effort tier mapping:
            basic / medium  → prefer balanced
            deep / expert   → prefer flagship (with balanced fallback)
            ultra           → flagship strongly preferred
        """
        preferred_strengths = ROLE_STRENGTH_MAP.get(role, [])
        tier_preference = list(
            _TIER_PREFERENCE.get(effort, ["balanced", "flagship", "lightweight"])
        )
        if require_tier:
            tier_preference = [require_tier] + [t for t in tier_preference if t != require_tier]

        def score(spec: ModelSpec) -> float:
            s = 0.0
            # Tier fit — closer to the front of the preference list = better.
            try:
                tier_idx = tier_preference.index(spec.tier)
            except ValueError:
                tier_idx = 99
            s += (10 - tier_idx) * 3.0
            # Strength match — count overlapping strengths.
            matched = sum(1 for st in preferred_strengths if st in spec.strengths)
            s += matched * 2.0
            # Benchmark quality — SWE-bench dominates, HumanEval is a tiebreak.
            s += spec.swebench_pct * 0.3
            s += spec.humaneval_pct * 0.05
            # Already-installed gets a huge bonus (avoid downloads).
            if self._tag_installed(spec.ollama_tag):
                s += 50.0
            return s

        ranked = sorted(CODE_MODEL_CATALOGUE, key=score, reverse=True)
        best = ranked[0]
        return best, self._tag_installed(best.ollama_tag)

    # ── High-level: ensure-or-pull ─────────────────────────────────────────

    async def ensure_model(
        self,
        role: str,
        effort: str = "medium",
        on_download_start: Callable[[ModelSpec], Awaitable[None]] | None = None,
        on_progress: ProgressCallback | None = None,
        on_download_complete: Callable[[ModelSpec], Awaitable[None]] | None = None,
    ) -> ModelSpec:
        """
        Guarantee a usable model for ``role`` at the requested effort tier.

        If the best candidate isn't installed, pull it (firing the
        provided callbacks for UI feedback). On pull failure, fall back
        to the best already-installed candidate (ranked by HumanEval).
        Raises ``RuntimeError`` only if absolutely nothing is installed
        AND the pull failed.
        """
        if not self._probed:
            await self.probe()

        spec, installed = self.select_model(role, effort)

        if not installed:
            if on_download_start:
                try:
                    await on_download_start(spec)
                except Exception:  # pragma: no cover
                    pass
            logger.info(
                "code_registry_auto_pull role=%s tag=%s effort=%s",
                role,
                spec.ollama_tag,
                effort,
            )
            success = await self.pull_model(spec.ollama_tag, on_progress=on_progress)
            if not success:
                # Fall back to the best already-installed candidate.
                for candidate in sorted(
                    CODE_MODEL_CATALOGUE,
                    key=lambda s: (s.swebench_pct, s.humaneval_pct),
                    reverse=True,
                ):
                    if self._tag_installed(candidate.ollama_tag):
                        logger.warning(
                            "code_registry_pull_failed_fallback tag=%s",
                            candidate.ollama_tag,
                        )
                        return candidate
                raise RuntimeError(
                    "No code-capable model is available in Ollama. "
                    "Pull at least one model first (e.g. "
                    "`ollama pull qwen2.5-coder:7b`)."
                )
            if on_download_complete:
                try:
                    await on_download_complete(spec)
                except Exception:  # pragma: no cover
                    pass

        return spec

    # ── Catalogue snapshots for the /models endpoint ───────────────────────

    def catalogue_with_status(self) -> list[dict[str, Any]]:
        """
        Return the full catalogue, each entry annotated with whether it
        is currently installed. Convenience for the GET /api/code/models
        endpoint.
        """
        out: list[dict[str, Any]] = []
        for spec in CODE_MODEL_CATALOGUE:
            entry = spec.to_dict()
            entry["installed"] = self._tag_installed(spec.ollama_tag)
            out.append(entry)
        return out
