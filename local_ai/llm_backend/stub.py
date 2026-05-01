"""
StubBackend — deterministic, no I/O (Phase 16 Commit A).

Used by the test suite to exercise call-site behaviour without
spinning up Ollama or any external service.  Behaviour:

* ``chat`` / ``complete`` return canned responses in round-robin
  order from the ``responses`` list.
* ``stream_chat`` yields one character at a time so streaming
  callers can be tested deterministically.
* Every call is recorded in ``self.calls`` for assertions.

License: MIT.
"""

from __future__ import annotations

from typing import AsyncGenerator, Optional

from .base import (
    ChatMessage,
    ChatOptions,
    ChatResponse,
    ChatUsage,
    LLMBackend,
    normalize_messages,
)


class StubBackend(LLMBackend):
    """No-I/O LLM backend for tests."""

    def __init__(
        self,
        *,
        responses: Optional[list[str]] = None,
        models: Optional[list[str]] = None,
    ) -> None:
        self._responses: list[str] = list(responses or ["stub-response"])
        self._idx = 0
        self._models: list[str] = list(models or ["stub-model"])
        self.calls: list[dict] = []

    @property
    def name(self) -> str:
        return "stub"

    # -- lifecycle ---------------------------------------------------

    async def health_check(self) -> bool:
        return True

    async def list_models(self) -> list[str]:
        return list(self._models)

    # -- helpers -----------------------------------------------------

    def _next(self) -> str:
        if not self._responses:
            return ""
        text = self._responses[self._idx % len(self._responses)]
        self._idx += 1
        return text

    def queue_response(self, text: str) -> None:
        """Append a response to the round-robin queue mid-test."""
        self._responses.append(text)

    def reset(self) -> None:
        self._idx = 0
        self.calls.clear()

    # -- inference ---------------------------------------------------

    async def chat(
        self,
        messages: list[ChatMessage] | list[dict],
        *,
        model: str,
        options: Optional[ChatOptions] = None,
    ) -> ChatResponse:
        norm = normalize_messages(messages)
        self.calls.append({
            "kind": "chat",
            "model": model,
            "messages": norm,
            "options": options,
        })
        text = self._next()
        return ChatResponse(
            content=text,
            model=model,
            finish_reason="stop",
            usage=ChatUsage(
                prompt_tokens=sum(len(m.get("content", "")) for m in norm),
                completion_tokens=len(text),
                total_tokens=sum(len(m.get("content", "")) for m in norm)
                + len(text),
            ),
            raw={"stub": True},
        )

    async def stream_chat(
        self,
        messages: list[ChatMessage] | list[dict],
        *,
        model: str,
        options: Optional[ChatOptions] = None,
    ) -> AsyncGenerator[str, None]:
        norm = normalize_messages(messages)
        self.calls.append({
            "kind": "stream",
            "model": model,
            "messages": norm,
            "options": options,
        })
        text = self._next()
        for ch in text:
            yield ch

    async def complete(
        self,
        prompt: str,
        *,
        model: str,
        system: Optional[str] = None,
        options: Optional[ChatOptions] = None,
    ) -> str:
        self.calls.append({
            "kind": "complete",
            "model": model,
            "prompt": prompt,
            "system": system,
            "options": options,
        })
        return self._next()


__all__ = ["StubBackend"]
