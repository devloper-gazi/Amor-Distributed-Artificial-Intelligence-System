"""
LlamaCppBackend — direct ``llama-server`` (llama.cpp) backend.

Phase 16 ships only the *client* side — a thin
``OpenAICompatibleBackend`` subclass that points at an already-running
``llama-server``.  Subprocess management (spawn / supervise) is
Phase 17 deployment work.

To use today, run ``llama-server`` externally::

    llama-server -m model.gguf --host 127.0.0.1 --port 8080

then set ``settings.llm_backend = "llama-cpp"`` and
``settings.llm_backend_url = "http://localhost:8080"`` (the ``/v1``
suffix is normalised in by the parent class).

License: MIT.
"""

from __future__ import annotations

from .openai_compat import OpenAICompatibleBackend


class LlamaCppBackend(OpenAICompatibleBackend):
    """OpenAI-shape client pointed at a llama.cpp ``llama-server``."""

    BACKEND_NAME = "llama-cpp"

    def __init__(
        self,
        *,
        base_url: str = "http://localhost:8080",
        api_key: str = "",
        timeout: float = 300.0,
    ) -> None:
        super().__init__(base_url=base_url, api_key=api_key, timeout=timeout)


__all__ = ["LlamaCppBackend"]
