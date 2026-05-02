"""
OpenAI-compatible facade — Phase 16 Commit C.

Mounts ``/v1/*`` endpoints on the existing FastAPI app so external
SDKs (Letta, OpenHands, Aider, the OpenAI Python SDK itself) can talk
to AMOR with a single env-var flip:

    OPENAI_BASE_URL=http://localhost:8000/v1
    OPENAI_API_KEY=any-non-empty-string

Endpoints
---------

* ``GET  /v1/models``                — list available models
* ``POST /v1/chat/completions``      — chat (sync + SSE streaming)
* ``POST /v1/completions``           — legacy completion (delegated
                                        to chat with a single user
                                        turn for backwards compat)
* ``POST /v1/embeddings``            — text embeddings (best-effort;
                                        503 when no embedder is wired)

The facade is a *pure forwarder* — it never hits Ollama directly.
Every request goes through ``local_ai.llm_backend.get_backend()`` so
flipping ``settings.llm_backend`` at runtime steers the facade too.

License: MIT.
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any, AsyncGenerator, Dict, List, Optional
from uuid import uuid4

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field

from ..auth.dependencies import get_optional_user
from ..auth.models import User
from ..config.settings import settings


logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────
# Models — closely mirror OpenAI's wire shape so SDKs Just Work.
# ─────────────────────────────────────────────────────────────────────


router = APIRouter(prefix="/v1", tags=["openai-compat"])


class ChatMessageBody(BaseModel):
    role: str = Field(..., max_length=20)
    content: str = Field("", max_length=200_000)
    name: Optional[str] = Field(None, max_length=200)
    tool_call_id: Optional[str] = Field(None, max_length=200)


class ChatCompletionsRequest(BaseModel):
    model: str = Field(..., min_length=1, max_length=200)
    messages: List[ChatMessageBody] = Field(..., min_length=1)
    temperature: float = Field(0.7, ge=0.0, le=2.0)
    top_p: float = Field(1.0, ge=0.0, le=1.0)
    max_tokens: Optional[int] = Field(None, ge=1, le=32000)
    seed: Optional[int] = Field(None)
    stop: Optional[List[str]] = None
    stream: bool = False


class CompletionsRequest(BaseModel):
    model: str = Field(..., min_length=1, max_length=200)
    prompt: str = Field("", max_length=200_000)
    temperature: float = Field(0.7, ge=0.0, le=2.0)
    max_tokens: Optional[int] = Field(None, ge=1, le=32000)
    stop: Optional[List[str]] = None
    stream: bool = False


class EmbeddingsRequest(BaseModel):
    model: str = Field("default", max_length=200)
    input: Any  # str | list[str] | list[int]


# ─────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────


def _check_enabled() -> None:
    """Master gate.  Mirrors ``sentinel_evolution`` style — flip
    ``settings.openai_compat_enabled = False`` to take the facade
    offline without restarting."""
    if not getattr(settings, "openai_compat_enabled", True):
        raise HTTPException(
            status_code=503,
            detail="OpenAI-compatible facade disabled "
                   "(settings.openai_compat_enabled=False)",
        )


def _backend_or_503():
    try:
        from local_ai.llm_backend import get_backend  # noqa: PLC0415
        return get_backend()
    except Exception as exc:  # pragma: no cover
        raise HTTPException(
            status_code=503,
            detail=f"LLM backend unavailable: {type(exc).__name__}: {exc}",
        )


def _to_chat_options(body: ChatCompletionsRequest):
    from local_ai.llm_backend import ChatOptions  # noqa: PLC0415
    return ChatOptions(
        temperature=body.temperature,
        top_p=body.top_p,
        max_tokens=body.max_tokens,
        seed=body.seed,
        stop=body.stop,
    )


def _completion_id() -> str:
    return f"chatcmpl-{uuid4().hex[:24]}"


def _now_ts() -> int:
    return int(time.time())


# ─────────────────────────────────────────────────────────────────────
# /v1/models
# ─────────────────────────────────────────────────────────────────────


@router.get("/models")
async def list_models(
    user: Optional[User] = Depends(get_optional_user),  # noqa: ARG001
    x_client_id: Optional[str] = Header(default=None, alias="X-Client-Id"),  # noqa: ARG001
) -> JSONResponse:
    _check_enabled()
    backend = _backend_or_503()
    try:
        names = await backend.list_models()
    except Exception as exc:
        logger.warning("openai-compat list_models failed: %s", exc)
        names = []
    data = [
        {
            "id": name,
            "object": "model",
            "created": _now_ts(),
            "owned_by": backend.name,
        }
        for name in names
    ]
    return JSONResponse({"object": "list", "data": data})


# ─────────────────────────────────────────────────────────────────────
# /v1/chat/completions
# ─────────────────────────────────────────────────────────────────────


def _chat_response_envelope(
    *,
    model: str,
    content: str,
    finish_reason: str,
    usage_in: int,
    usage_out: int,
) -> dict:
    return {
        "id": _completion_id(),
        "object": "chat.completion",
        "created": _now_ts(),
        "model": model,
        "choices": [{
            "index": 0,
            "message": {"role": "assistant", "content": content},
            "finish_reason": finish_reason,
        }],
        "usage": {
            "prompt_tokens": usage_in,
            "completion_tokens": usage_out,
            "total_tokens": usage_in + usage_out,
        },
    }


def _stream_chunk_envelope(
    *,
    completion_id: str,
    model: str,
    delta_content: Optional[str],
    finish_reason: Optional[str] = None,
) -> dict:
    delta: dict = {}
    if delta_content is not None:
        delta["content"] = delta_content
    return {
        "id": completion_id,
        "object": "chat.completion.chunk",
        "created": _now_ts(),
        "model": model,
        "choices": [{
            "index": 0,
            "delta": delta,
            "finish_reason": finish_reason,
        }],
    }


@router.post("/chat/completions")
async def chat_completions(
    body: ChatCompletionsRequest,
    request: Request,  # noqa: ARG001
    user: Optional[User] = Depends(get_optional_user),  # noqa: ARG001
    x_client_id: Optional[str] = Header(default=None, alias="X-Client-Id"),  # noqa: ARG001
):
    _check_enabled()
    backend = _backend_or_503()
    options = _to_chat_options(body)
    msgs: List[dict] = []
    for m in body.messages:
        d: dict = {"role": m.role, "content": m.content}
        if m.name is not None:
            d["name"] = m.name
        if m.tool_call_id is not None:
            d["tool_call_id"] = m.tool_call_id
        msgs.append(d)

    if not body.stream:
        try:
            resp = await backend.chat(msgs, model=body.model, options=options)
        except Exception as exc:
            logger.exception("chat_completions backend failure")
            raise HTTPException(
                status_code=503,
                detail=f"backend error: {type(exc).__name__}: {exc}",
            )
        return JSONResponse(_chat_response_envelope(
            model=resp.model,
            content=resp.content,
            finish_reason=resp.finish_reason or "stop",
            usage_in=resp.usage.prompt_tokens,
            usage_out=resp.usage.completion_tokens,
        ))

    # Streaming branch — emit OpenAI-shaped SSE.
    completion_id = _completion_id()

    async def _sse() -> AsyncGenerator[str, None]:
        try:
            async for chunk in backend.stream_chat(
                msgs, model=body.model, options=options,
            ):
                envelope = _stream_chunk_envelope(
                    completion_id=completion_id,
                    model=body.model,
                    delta_content=chunk,
                )
                yield f"data: {json.dumps(envelope)}\n\n"
        except Exception as exc:
            logger.exception("chat_completions stream failure")
            error_env = {
                "error": {
                    "message": f"{type(exc).__name__}: {exc}",
                    "type": "backend_error",
                },
            }
            yield f"data: {json.dumps(error_env)}\n\n"
        # Final stop chunk + DONE marker.
        yield "data: " + json.dumps(_stream_chunk_envelope(
            completion_id=completion_id,
            model=body.model,
            delta_content=None,
            finish_reason="stop",
        )) + "\n\n"
        yield "data: [DONE]\n\n"

    return StreamingResponse(
        _sse(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


# ─────────────────────────────────────────────────────────────────────
# /v1/completions  (legacy; we forward to chat)
# ─────────────────────────────────────────────────────────────────────


@router.post("/completions")
async def completions(
    body: CompletionsRequest,
    user: Optional[User] = Depends(get_optional_user),  # noqa: ARG001
    x_client_id: Optional[str] = Header(default=None, alias="X-Client-Id"),  # noqa: ARG001
) -> JSONResponse:
    _check_enabled()
    backend = _backend_or_503()
    from local_ai.llm_backend import ChatOptions  # noqa: PLC0415
    options = ChatOptions(
        temperature=body.temperature,
        max_tokens=body.max_tokens,
        stop=body.stop,
    )
    if body.stream:
        # Legacy /v1/completions streaming is rarely used today; we
        # surface a clear error rather than silently downgrading.
        raise HTTPException(
            status_code=400,
            detail="streaming /v1/completions is not supported; use /v1/chat/completions",
        )
    msgs = [{"role": "user", "content": body.prompt}]
    try:
        resp = await backend.chat(msgs, model=body.model, options=options)
    except Exception as exc:
        logger.exception("/v1/completions backend failure")
        raise HTTPException(
            status_code=503,
            detail=f"backend error: {type(exc).__name__}: {exc}",
        )
    return JSONResponse({
        "id": f"cmpl-{uuid4().hex[:24]}",
        "object": "text_completion",
        "created": _now_ts(),
        "model": resp.model,
        "choices": [{
            "index": 0,
            "text": resp.content,
            "finish_reason": resp.finish_reason or "stop",
        }],
        "usage": {
            "prompt_tokens": resp.usage.prompt_tokens,
            "completion_tokens": resp.usage.completion_tokens,
            "total_tokens": resp.usage.total_tokens,
        },
    })


# ─────────────────────────────────────────────────────────────────────
# /v1/embeddings  (best-effort)
# ─────────────────────────────────────────────────────────────────────


@router.post("/embeddings")
async def embeddings(
    body: EmbeddingsRequest,
    user: Optional[User] = Depends(get_optional_user),  # noqa: ARG001
    x_client_id: Optional[str] = Header(default=None, alias="X-Client-Id"),  # noqa: ARG001
) -> JSONResponse:
    """Embeddings facade.

    Phase 16 doesn't ship a unified ``Embedder`` — those live in
    Commit D2 once BGE-M3 lands.  Until then this endpoint either
    forwards to the active backend's embedder when one is exposed
    (Ollama provides ``/api/embeddings``) or returns 503.
    """
    _check_enabled()
    backend = _backend_or_503()

    # Normalise input to list[str].
    raw = body.input
    if isinstance(raw, str):
        inputs = [raw]
    elif isinstance(raw, list):
        inputs = [str(x) for x in raw]
    else:
        raise HTTPException(400, "input must be a string or list of strings")

    # Backend-specific best-effort embedding via Ollama's /api/embeddings.
    if getattr(backend, "name", "") != "ollama":
        raise HTTPException(
            status_code=503,
            detail="embeddings facade requires the Ollama backend in "
                   "Phase 16; BGE-M3 unified embedder lands in Commit D2",
        )
    try:
        from local_ai.ollama_client import OllamaClient  # noqa: PLC0415
        client = OllamaClient(base_url=backend.base_url, model=body.model)
        vectors: List[List[float]] = []
        for text in inputs:
            v = await client.embeddings(text)
            vectors.append([float(x) for x in v])
    except Exception as exc:
        logger.exception("/v1/embeddings backend failure")
        raise HTTPException(
            status_code=503,
            detail=f"embeddings error: {type(exc).__name__}: {exc}",
        )
    return JSONResponse({
        "object": "list",
        "data": [
            {"object": "embedding", "embedding": v, "index": i}
            for i, v in enumerate(vectors)
        ],
        "model": body.model,
        "usage": {
            "prompt_tokens": sum(len(t) for t in inputs),
            "total_tokens": sum(len(t) for t in inputs),
        },
    })
