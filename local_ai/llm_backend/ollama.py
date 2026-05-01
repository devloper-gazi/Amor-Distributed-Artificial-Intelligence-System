"""
OllamaBackend — the default ``LLMBackend`` (Phase 16 Commit A).

Faithful refactor of the bespoke ``httpx.post(/api/generate)`` paths
that lived inside ``call_ollama_with`` (`local_ai_routes_simple.py`)
and ``OllamaClient`` (`ollama_client.py`).  Behaviour is intended to
be byte-equivalent for the existing test suite — Commit B replaces
direct HTTP calls with ``backend.chat()`` / ``backend.complete()``.

License: MIT.
"""

from __future__ import annotations

import json
import logging
from typing import AsyncGenerator, Optional

import httpx

from .base import (
    ChatMessage,
    ChatOptions,
    ChatResponse,
    ChatUsage,
    LLMBackend,
    normalize_messages,
)


logger = logging.getLogger(__name__)


_DEFAULT_TIMEOUT = 300.0  # seconds — matches OllamaClient default


class OllamaBackend(LLMBackend):
    """LLM backend backed by an Ollama daemon."""

    def __init__(
        self,
        *,
        base_url: str = "http://localhost:11434",
        timeout: float = _DEFAULT_TIMEOUT,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = float(timeout)

    # ─── identity ───────────────────────────────────────────────

    @property
    def name(self) -> str:
        return "ollama"

    def __repr__(self) -> str:  # pragma: no cover - cosmetic
        return f"OllamaBackend(base_url={self.base_url!r})"

    # ─── lifecycle ──────────────────────────────────────────────

    async def health_check(self) -> bool:
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                resp = await client.get(f"{self.base_url}/api/tags")
                return resp.status_code == 200
        except Exception as exc:  # pragma: no cover - infra path
            logger.debug("ollama health check failed: %s", exc)
            return False

    async def list_models(self) -> list[str]:
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.get(f"{self.base_url}/api/tags")
                if resp.status_code != 200:
                    return []
                payload = resp.json()
                models = payload.get("models") or []
                return [
                    m.get("name") or m.get("model") or ""
                    for m in models
                    if isinstance(m, dict)
                ]
        except Exception as exc:  # pragma: no cover
            logger.debug("ollama list_models failed: %s", exc)
            return []

    async def unload_model(self, model: str) -> bool:
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                await client.post(
                    f"{self.base_url}/api/generate",
                    json={
                        "model": model,
                        "prompt": "",
                        "keep_alive": "0",
                    },
                )
            return True
        except Exception as exc:  # pragma: no cover
            logger.debug("ollama unload_model failed: %s", exc)
            return False

    # ─── inference ──────────────────────────────────────────────

    def _build_options(self, opts: ChatOptions) -> dict:
        out: dict = {
            "temperature": float(opts.temperature),
            "top_p": float(opts.top_p),
        }
        if opts.max_tokens is not None:
            out["num_predict"] = int(opts.max_tokens)
        if opts.seed is not None:
            out["seed"] = int(opts.seed)
        if opts.stop:
            out["stop"] = list(opts.stop)
        if opts.extra:
            out.update(dict(opts.extra))
        return out

    async def chat(
        self,
        messages: list[ChatMessage] | list[dict],
        *,
        model: str,
        options: Optional[ChatOptions] = None,
    ) -> ChatResponse:
        opts = options or ChatOptions()
        body: dict = {
            "model": model,
            "messages": normalize_messages(messages),
            "stream": False,
            "options": self._build_options(opts),
        }
        if opts.keep_alive is not None:
            body["keep_alive"] = opts.keep_alive

        async with httpx.AsyncClient(timeout=self.timeout) as client:
            resp = await client.post(
                f"{self.base_url}/api/chat", json=body,
            )
            resp.raise_for_status()
            data = resp.json()

        msg = data.get("message") or {}
        return ChatResponse(
            content=msg.get("content", ""),
            model=data.get("model", model),
            finish_reason=data.get("done_reason", "stop"),
            usage=ChatUsage(
                prompt_tokens=int(data.get("prompt_eval_count") or 0),
                completion_tokens=int(data.get("eval_count") or 0),
                total_tokens=(
                    int(data.get("prompt_eval_count") or 0)
                    + int(data.get("eval_count") or 0)
                ),
            ),
            raw=data,
        )

    async def stream_chat(
        self,
        messages: list[ChatMessage] | list[dict],
        *,
        model: str,
        options: Optional[ChatOptions] = None,
    ) -> AsyncGenerator[str, None]:
        opts = options or ChatOptions()
        body: dict = {
            "model": model,
            "messages": normalize_messages(messages),
            "stream": True,
            "options": self._build_options(opts),
        }
        if opts.keep_alive is not None:
            body["keep_alive"] = opts.keep_alive

        async with httpx.AsyncClient(timeout=self.timeout) as client:
            async with client.stream(
                "POST", f"{self.base_url}/api/chat", json=body,
            ) as resp:
                resp.raise_for_status()
                async for line in resp.aiter_lines():
                    if not line:
                        continue
                    try:
                        data = json.loads(line)
                    except Exception:  # pragma: no cover
                        continue
                    msg = data.get("message") or {}
                    chunk = msg.get("content") or ""
                    if chunk:
                        yield chunk
                    if data.get("done"):
                        break

    async def complete(
        self,
        prompt: str,
        *,
        model: str,
        system: Optional[str] = None,
        options: Optional[ChatOptions] = None,
    ) -> str:
        opts = options or ChatOptions()
        body: dict = {
            "model": model,
            "prompt": prompt,
            "system": system or "",
            "stream": False,
            "options": self._build_options(opts),
        }
        if opts.keep_alive is not None:
            body["keep_alive"] = opts.keep_alive

        async with httpx.AsyncClient(timeout=self.timeout) as client:
            resp = await client.post(
                f"{self.base_url}/api/generate", json=body,
            )
            resp.raise_for_status()
            return resp.json().get("response", "")


__all__ = ["OllamaBackend"]
