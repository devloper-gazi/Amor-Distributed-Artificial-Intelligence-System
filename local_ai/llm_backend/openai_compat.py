"""
OpenAICompatibleBackend — generic ``/v1/chat/completions`` client.

Works with any server that implements the OpenAI inference API:

* vLLM
* ExLlamaV2's ``oai-server``
* llama.cpp's ``llama-server`` (when started with ``--api-key`` etc.)
* llama-swap proxy (subclassed in ``llama_swap.py``)
* LM Studio's ``llmster`` daemon
* Locally hosted Anthropic / Mistral / Together / etc. shims

The base URL must point at the *root* of the API surface — e.g.
``http://localhost:8080/v1``.  The trailing slash is normalised away.

This class is the parent for ``LlamaSwapBackend`` and
``LlamaCppBackend`` (different defaults + names; identical wire
shape).

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


class OpenAICompatibleBackend(LLMBackend):
    """Generic OpenAI ``/v1`` client."""

    BACKEND_NAME = "openai-compat"

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str = "",
        timeout: float = 300.0,
    ) -> None:
        # Defensive: accept both ``http://host:port`` and ``http://host:port/v1``.
        url = base_url.rstrip("/")
        if not url.endswith("/v1"):
            url = url + "/v1"
        self.base_url = url
        self.api_key = api_key or ""
        self.timeout = float(timeout)

    @property
    def name(self) -> str:
        return self.BACKEND_NAME

    def __repr__(self) -> str:  # pragma: no cover - cosmetic
        return f"{type(self).__name__}(base_url={self.base_url!r})"

    # ─── headers ────────────────────────────────────────────────

    def _headers(self) -> dict:
        h = {"Content-Type": "application/json"}
        if self.api_key:
            h["Authorization"] = f"Bearer {self.api_key}"
        return h

    # ─── lifecycle ──────────────────────────────────────────────

    async def health_check(self) -> bool:
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                resp = await client.get(
                    f"{self.base_url}/models",
                    headers=self._headers(),
                )
                # 200 = OK, 401 = reachable but unauthenticated.
                return resp.status_code in (200, 401)
        except Exception as exc:  # pragma: no cover
            logger.debug("%s health check failed: %s", self.name, exc)
            return False

    async def list_models(self) -> list[str]:
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.get(
                    f"{self.base_url}/models",
                    headers=self._headers(),
                )
                if resp.status_code != 200:
                    return []
                data = resp.json()
                items = data.get("data") or data.get("models") or []
                names: list[str] = []
                for m in items:
                    if isinstance(m, dict):
                        nm = m.get("id") or m.get("name")
                        if nm:
                            names.append(str(nm))
                    elif isinstance(m, str):
                        names.append(m)
                return names
        except Exception as exc:  # pragma: no cover
            logger.debug("%s list_models failed: %s", self.name, exc)
            return []

    # ─── inference ──────────────────────────────────────────────

    def _build_body(
        self,
        messages: list[ChatMessage] | list[dict],
        model: str,
        options: Optional[ChatOptions],
        *,
        stream: bool,
    ) -> dict:
        opts = options or ChatOptions()
        body: dict = {
            "model": model,
            "messages": normalize_messages(messages),
            "temperature": float(opts.temperature),
            "top_p": float(opts.top_p),
            "stream": bool(stream),
        }
        if opts.max_tokens is not None:
            body["max_tokens"] = int(opts.max_tokens)
        if opts.seed is not None:
            body["seed"] = int(opts.seed)
        if opts.stop:
            body["stop"] = list(opts.stop)
        if opts.extra:
            body.update(dict(opts.extra))
        return body

    async def chat(
        self,
        messages: list[ChatMessage] | list[dict],
        *,
        model: str,
        options: Optional[ChatOptions] = None,
    ) -> ChatResponse:
        body = self._build_body(messages, model, options, stream=False)
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            resp = await client.post(
                f"{self.base_url}/chat/completions",
                json=body,
                headers=self._headers(),
            )
            resp.raise_for_status()
            data = resp.json()
        choices = data.get("choices") or []
        if not choices:
            return ChatResponse(content="", model=data.get("model", model), raw=data)
        choice = choices[0] or {}
        msg = choice.get("message") or {}
        usage = data.get("usage") or {}
        return ChatResponse(
            content=msg.get("content", "") or "",
            model=data.get("model", model),
            finish_reason=choice.get("finish_reason", "stop") or "stop",
            usage=ChatUsage(
                prompt_tokens=int(usage.get("prompt_tokens", 0) or 0),
                completion_tokens=int(usage.get("completion_tokens", 0) or 0),
                total_tokens=int(usage.get("total_tokens", 0) or 0),
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
        body = self._build_body(messages, model, options, stream=True)
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            async with client.stream(
                "POST",
                f"{self.base_url}/chat/completions",
                json=body,
                headers=self._headers(),
            ) as resp:
                resp.raise_for_status()
                async for line in resp.aiter_lines():
                    if not line:
                        continue
                    if not line.startswith("data:"):
                        continue
                    payload = line[len("data:"):].strip()
                    if payload == "[DONE]":
                        break
                    try:
                        chunk = json.loads(payload)
                    except Exception:  # pragma: no cover
                        continue
                    choices = chunk.get("choices") or []
                    if not choices:
                        continue
                    delta = choices[0].get("delta") or {}
                    content = delta.get("content") or ""
                    if content:
                        yield content

    async def complete(
        self,
        prompt: str,
        *,
        model: str,
        system: Optional[str] = None,
        options: Optional[ChatOptions] = None,
    ) -> str:
        # OpenAI's legacy ``/v1/completions`` is largely deprecated;
        # use a single-turn chat instead.
        msgs: list[dict] = []
        if system:
            msgs.append({"role": "system", "content": system})
        msgs.append({"role": "user", "content": prompt})
        resp = await self.chat(msgs, model=model, options=options)
        return resp.content


__all__ = ["OpenAICompatibleBackend"]
