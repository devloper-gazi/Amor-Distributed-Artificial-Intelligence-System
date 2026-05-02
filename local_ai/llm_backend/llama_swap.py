"""
LlamaSwapBackend — proxy that hot-swaps ``llama-server`` instances on
a single OpenAI-compatible port.

Phase 16 ships only the *client* side (a thin ``OpenAICompatibleBackend``
subclass with a labelled name and a llama-swap-friendly default port).
Subprocess lifecycle (spawn / supervise / health-loop the
``llama-swap`` binary) is Phase 17 deployment work.

To use today, run llama-swap externally::

    llama-swap --listen :11435 --config llama-swap.yaml

then set ``settings.llm_backend = "llama-swap"`` and
``settings.llm_backend_url = "http://localhost:11435"``.

License: MIT.
"""

from __future__ import annotations

from .openai_compat import OpenAICompatibleBackend


class LlamaSwapBackend(OpenAICompatibleBackend):
    """OpenAI-shape client pointed at a llama-swap proxy."""

    BACKEND_NAME = "llama-swap"

    def __init__(
        self,
        *,
        base_url: str = "http://localhost:11435",
        api_key: str = "",
        timeout: float = 300.0,
    ) -> None:
        super().__init__(base_url=base_url, api_key=api_key, timeout=timeout)


__all__ = ["LlamaSwapBackend"]
