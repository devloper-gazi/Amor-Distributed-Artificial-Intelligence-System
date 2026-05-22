"""
Pluggable LLM backend — Phase 16 Commit A: ABC + dataclass contracts.

Every concrete backend (OllamaBackend, LlamaSwapBackend, LlamaCppBackend,
OpenAICompatibleBackend, StubBackend) lives in its own module and is
constructed lazily via ``llm_backend.make_backend(kind)``.  Existing
call sites migrate to ``get_backend().chat(...)`` in Commit B; this
commit ships only the ABC and the two backends needed for tests.

Design notes
------------
* The ABC mirrors the OpenAI ``/v1/chat/completions`` shape so future
  external SDKs (Letta / OpenHands / Aider) plug in with a single
  ``OPENAI_BASE_URL`` pointing at the OpenAI-compat facade (Commit C).
* ``ChatOptions`` carries an ``extra: dict`` escape hatch for
  backend-specific knobs (Ollama's ``num_ctx``, llama.cpp's
  ``n_keep``, etc.) without polluting the contract.
* ``Embedder`` is a separate ABC because not every LLM backend
  serves embeddings; the embedding model is usually a different
  process (sentence-transformers on CPU).

License: MIT.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, AsyncGenerator, Iterable, Optional


# ─── dataclasses ────────────────────────────────────────────────────


@dataclass
class ChatMessage:
    """OpenAI-shaped chat turn."""

    role: str  # system | user | assistant | tool
    content: str
    name: Optional[str] = None
    tool_call_id: Optional[str] = None
    # Cycle UI v2.7.2 (D7) — multimodal image attachments.  Each
    # element is a base64-encoded image payload (PNG/JPEG/WebP),
    # NO data: prefix — matches Ollama's `/api/chat` schema natively
    # and the OpenAI vision spec via openai_compat backend's
    # `content` array conversion.  Backends without vision support
    # silently drop this field (graceful degradation).
    images: Optional[list[str]] = None


@dataclass
class ChatUsage:
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0


@dataclass
class ChatChoice:
    index: int
    message: ChatMessage
    finish_reason: str = "stop"


@dataclass
class ChatResponse:
    """Normalised response.  Backends populate as much as they can;
    fields they don't know stay at defaults.  ``raw`` carries the
    backend-specific payload so callers can introspect."""

    content: str
    model: str
    finish_reason: str = "stop"
    usage: ChatUsage = field(default_factory=ChatUsage)
    raw: dict = field(default_factory=dict)


@dataclass
class ChatOptions:
    """Sampling + lifecycle knobs.  ``extra`` is a per-backend escape
    hatch for keys we don't normalise (top_k, frequency_penalty,
    presence_penalty, n_keep, num_ctx, mirostat, etc.)."""

    temperature: float = 0.7
    max_tokens: Optional[int] = None
    top_p: float = 1.0
    seed: Optional[int] = None
    stop: Optional[list[str]] = None
    keep_alive: Optional[str] = None  # ollama-specific lifecycle hint
    extra: dict = field(default_factory=dict)


# ─── helpers ────────────────────────────────────────────────────────


def normalize_messages(
    messages: Iterable[ChatMessage] | Iterable[dict],
) -> list[dict]:
    """Coerce mixed input (dataclass or raw dict) into OpenAI-shaped
    dicts ready for transport.  Raises ``TypeError`` on unknown types
    so a misuse fails loudly instead of silently dropping content."""
    out: list[dict] = []
    for m in messages:
        if isinstance(m, ChatMessage):
            d: dict[str, Any] = {"role": m.role, "content": m.content}
            if m.name is not None:
                d["name"] = m.name
            if m.tool_call_id is not None:
                d["tool_call_id"] = m.tool_call_id
            # Cycle UI v2.7.2 (D7) — image array carried through the
            # transport.  Backends that understand the field (Ollama
            # multimodal models, OpenAI vision-compat) consume it;
            # text-only backends ignore unknown keys.
            if m.images:
                d["images"] = list(m.images)
            out.append(d)
        elif isinstance(m, dict):
            if "role" not in m or "content" not in m:
                raise ValueError(
                    f"message dict missing required keys: {sorted(m)}"
                )
            out.append(dict(m))  # defensive copy
        else:
            raise TypeError(f"unsupported message type: {type(m).__name__}")
    return out


# ─── ABCs ───────────────────────────────────────────────────────────


class LLMBackend(ABC):
    """Pluggable LLM inference backend.

    Concrete classes live in sibling modules:

    * ``ollama.py``        — Ollama HTTP daemon (today's default)
    * ``llama_swap.py``    — llama-swap proxy (OpenAI ``/v1`` shape)
    * ``llama_cpp.py``     — direct ``llama-server`` (OpenAI shape)
    * ``openai_compat.py`` — generic OpenAI-compatible (vLLM, ExLlamaV2…)
    * ``stub.py``          — deterministic, no I/O (tests)
    """

    # -- identity ----------------------------------------------------

    @property
    @abstractmethod
    def name(self) -> str:
        """Stable backend identifier — must match what
        ``llm_backend.make_backend(kind)`` accepts as ``kind``."""
        ...

    # -- lifecycle ---------------------------------------------------

    @abstractmethod
    async def health_check(self) -> bool:
        """Return ``True`` when the backend is reachable.  Must not
        raise — callers use this to decide between backends."""
        ...

    @abstractmethod
    async def list_models(self) -> list[str]:
        """Return the models the backend can serve right now.
        Empty list on failure (must not raise)."""
        ...

    async def unload_model(self, model: str) -> bool:  # noqa: ARG002
        """Optional VRAM hint.  Backends without an explicit notion
        of "loaded" return ``True`` and do nothing."""
        return True

    # -- inference ---------------------------------------------------

    @abstractmethod
    async def chat(
        self,
        messages: list[ChatMessage] | list[dict],
        *,
        model: str,
        options: Optional[ChatOptions] = None,
    ) -> ChatResponse:
        """Synchronous chat completion."""
        ...

    @abstractmethod
    def stream_chat(
        self,
        messages: list[ChatMessage] | list[dict],
        *,
        model: str,
        options: Optional[ChatOptions] = None,
    ) -> AsyncGenerator[str, None]:
        """Streaming chat completion — yields content chunks.  Note
        this is *not* an ``async def`` (it returns the generator).
        Concrete implementations are ``async def`` with ``yield``."""
        ...

    @abstractmethod
    async def complete(
        self,
        prompt: str,
        *,
        model: str,
        system: Optional[str] = None,
        options: Optional[ChatOptions] = None,
    ) -> str:
        """Legacy completion path — used by the existing
        ``_llm_call_local`` bridge.  Returns just the text."""
        ...


class Embedder(ABC):
    """Embedding-model backend.  Decoupled from ``LLMBackend`` because
    the embedder usually runs on CPU (sentence-transformers) while
    the LLM lives on GPU."""

    @property
    @abstractmethod
    def name(self) -> str:
        ...

    @property
    @abstractmethod
    def dimension(self) -> int:
        ...

    @abstractmethod
    async def embed(
        self, text: str | list[str],
    ) -> list[list[float]]:
        """Return one vector per input string.  A single ``str``
        returns ``[[...]]`` (one vector wrapped in a list) so callers
        can treat the result uniformly."""
        ...


__all__ = [
    "ChatMessage",
    "ChatChoice",
    "ChatUsage",
    "ChatResponse",
    "ChatOptions",
    "LLMBackend",
    "Embedder",
    "normalize_messages",
]
