"""
Cycle H Phase A.1 — BitNet b1.58 2B4T CPU shadow planner backend.

Wraps Microsoft's bitnet.cpp llama-server in the same OpenAI-compatible
client shape used by `LlamaCppBackend` / `OpenAICompatibleBackend`.

Why a separate backend class
----------------------------
bitnet.cpp IS a fork of llama.cpp and exposes the same `/v1/chat/completions`
+ `/v1/models` endpoints — so functionally a `LlamaCppBackend` pointed at
the bitnet.cpp server would work.  We ship a distinct class because:

  1. Default URL differs (`:8081` to avoid clashing with llama-swap on `9100`
     and the llama-cpp default `8080`).
  2. Shadow-mode routing logic in `code_intelligence/planners.py` needs a
     discriminable `BACKEND_NAME` ("bitnet-cpu") for telemetry +
     agreement-rate measurement against the main planner.
  3. Plan-agent locked timeout: 8s p99 fallback (BitNet 6-10 tok/s on CPU
     single-threaded means a 200-token plan can take 20-30s without a
     hard cap — the OpenAI-compat parent's default 300s is way too lax).
  4. License posture: BitNet b1.58 2B4T ships under MIT (Microsoft);
     LlamaCppBackend's GGUFs ship under their own licenses — keeping the
     class distinct lets the model_registry license-audit fields stay
     accurate.

Setup (operator-side, ONE-TIME)
-------------------------------
1. Clone bitnet.cpp: `git clone https://github.com/microsoft/BitNet`
2. Build: `cmake -B build && cmake --build build --target llama-server`
3. Download GGUF:
   `hf download microsoft/bitnet-b1.58-2B-4T-gguf --local-dir models/bitnet`
4. Run server:
   `llama-server -m models/bitnet/ggml-model-i2_s.gguf
                 --host 127.0.0.1 --port 8081
                 --ctx-size 4096 --threads 8`
5. AMOR side: set `code_bitnet_planner_enabled=True` +
   `code_bitnet_planner_url=http://localhost:8081` and the existing
   shadow-routing helper picks up the new backend.

The build is not bundled into AMOR's docker stack — bitnet.cpp wants
direct CPU access without the docker overhead, and the 0.4 GB model
weight is operator-side.  An optional `docker-compose.bitnet.yml`
overlay is documented in the operator runbook for hosts that DO want
containerised BitNet.
"""

from __future__ import annotations

from typing import Optional

from .openai_compat import OpenAICompatibleBackend


class BitNetCpuBackend(OpenAICompatibleBackend):
    """OpenAI-shape client pointed at a bitnet.cpp ``llama-server``."""

    BACKEND_NAME = "bitnet-cpu"

    #: Default port differs from llama-cpp (8080) and llama-swap (9100)
    #: so an operator running all three side-by-side doesn't get a
    #: bind conflict.
    DEFAULT_URL = "http://localhost:8081"

    #: Plan-agent locked: 8s hard timeout for the shadow path.  BitNet
    #: realistic CPU throughput is 6-10 tok/s on RTX 4060 laptop class;
    #: a 200-token plan worst-case at 6 tok/s is 33s, which the parent
    #: class's 300s would let bleed into the user's request budget.
    #: Shadow mode must NEVER block — fallback is `code_bitnet_fallback_to_main`.
    DEFAULT_TIMEOUT_S = 8.0

    def __init__(
        self,
        *,
        base_url: Optional[str] = None,
        api_key: str = "",
        timeout: Optional[float] = None,
    ) -> None:
        super().__init__(
            base_url=base_url or self.DEFAULT_URL,
            api_key=api_key,
            timeout=timeout if timeout is not None else self.DEFAULT_TIMEOUT_S,
        )


__all__ = ["BitNetCpuBackend"]
