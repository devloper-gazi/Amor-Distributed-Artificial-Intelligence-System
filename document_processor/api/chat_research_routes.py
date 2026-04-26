"""
Chat-based Research API Routes
Handles both Claude API and Local AI research through a unified chat interface
"""

import asyncio
import logging
import os
from typing import Optional, List
from datetime import datetime

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel, Field
from anthropic import AsyncAnthropic

from ..auth.dependencies import get_current_user
from ..auth.models import User
from ._query_persistence import (
    cancel_active_task,
    mark_query_cancelled,
    mark_query_completed,
    mark_query_failed,
    persist_assistant_message,
    persist_user_message,
    register_active_task,
    unregister_active_task,
)

logger = logging.getLogger(__name__)

# Create router
router = APIRouter(prefix="/api/chat", tags=["chat-research"])

# Initialize Claude client
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")
anthropic_client = AsyncAnthropic(api_key=ANTHROPIC_API_KEY) if ANTHROPIC_API_KEY else None


# Request/Response Models
class ChatHistoryMessage(BaseModel):
    """Minimal chat history message passed from the web UI."""

    role: str = Field(..., description="Message role: 'user' or 'assistant'")
    content: str = Field(..., description="Plain-text message content")


class ChatResearchRequest(BaseModel):
    """Unified request model for Claude research/thinking/coding modes.

    This is intentionally aligned with what `chat-research.js` sends so that:
    - `prompt`, `mode`, and `history` are available for all modes
    - research mode can optionally pass depth/translation settings
    - Phase C1+C2 fields connect the request to a chat session + query
      record so the server can mirror the frontend's persistence and
      become independently cancellable.
    """

    prompt: str = Field(..., min_length=1, description="User prompt or question")

    # Generic chat settings
    mode: Optional[str] = Field(
        "research",
        description="Interaction mode: 'research', 'thinking', or 'coding'",
    )
    history: List[ChatHistoryMessage] = Field(
        default_factory=list,
        description="Prior messages in this chat session for Claude context",
    )

    # Research-specific settings (used when mode == 'research')
    use_research: bool = Field(
        True, description="Enable research mode with web search when supported"
    )
    depth: Optional[str] = Field(
        default=None,
        description="Requested research depth (e.g. 'quick', 'standard', 'deep')",
    )
    use_translation: Optional[bool] = Field(
        default=True,
        description="Whether results should be translated to the target language",
    )
    target_language: Optional[str] = Field(
        default="en", description="Target language for translated research results"
    )

    # Generation controls
    max_tokens: int = Field(4096, description="Maximum tokens in response")
    temperature: float = Field(
        0.7, ge=0.0, le=1.0, description="Temperature for generation"
    )

    # ─── Phase C1+C2 — persistence + cancellation linkage ──────────
    chat_session_id: Optional[str] = Field(
        None, max_length=64,
        description="Chat session id this message belongs to (for server-side persist)",
    )
    query_record_id: Optional[str] = Field(
        None, max_length=64,
        description="Query record id (created by POST /api/query-records)",
    )
    user_message_idempotency_key: Optional[str] = Field(
        None, max_length=64,
        description="Dedupe key for the user message — same key sent by frontend persist",
    )
    assistant_message_idempotency_key: Optional[str] = Field(
        None, max_length=64,
        description="Dedupe key for the assistant message",
    )


class ChatResearchResponse(BaseModel):
    response: str = Field(..., description="Generated response")
    sources: Optional[List[dict]] = Field(None, description="Source citations if research was used")
    metadata: Optional[dict] = Field(None, description="Additional metadata")
    timestamp: str = Field(default_factory=lambda: datetime.utcnow().isoformat())


# ─── Phase C — shared wrap-around persistence + cancellation ────────
#
# Each of chat_research / chat_thinking / chat_coding wraps its Claude
# call in this helper so the on-success / on-failure / on-cancel
# bookkeeping (message persist + query_record stamping + active-task
# registry) lives in exactly one place. Handlers stay focused on
# building the system prompt + messages list.


async def _run_claude_with_persistence(
    *,
    request: ChatResearchRequest,
    user: User,
    x_client_id: Optional[str],
    system_prompt: str,
    ai_type: str,
):
    """
    Run a Claude call with full persistence + cancellation lifecycle.

    Behaviour matrix (always):
      * Persist user msg → run Claude → persist assistant msg → mark
        query_record completed.
      * On exception → mark query_record failed; re-raise as 500.
      * On asyncio.CancelledError → mark query_record cancelled; raise
        499 (RFC-7231-style "Client Closed Request").
    """
    if not anthropic_client:
        raise HTTPException(
            status_code=503,
            detail="Claude API not configured. Please set ANTHROPIC_API_KEY environment variable.",
        )

    client_id = (x_client_id or "").strip()
    chat_session_id = request.chat_session_id
    query_record_id = request.query_record_id

    # 1. Persist the user message immediately. The frontend writes the
    #    same message with the same idempotency_key — the unique sparse
    #    index collapses the two writes to one row.
    await persist_user_message(
        chat_session_id=chat_session_id,
        user_id=user.id,
        client_id=client_id,
        prompt=request.prompt,
        idempotency_key=request.user_message_idempotency_key,
    )

    # 2. Build conversation history.
    messages = []
    for msg in request.history:
        role = "assistant" if msg.role == "assistant" else "user"
        messages.append({"role": role, "content": msg.content})
    messages.append({"role": "user", "content": request.prompt})

    # 3. Run Claude as a tracked asyncio task so the cancel endpoint
    #    can target it.
    async def _do_call():
        return await anthropic_client.messages.create(
            model="claude-3.5-sonnet-latest",
            max_tokens=request.max_tokens,
            temperature=request.temperature,
            system=system_prompt,
            messages=messages,
        )

    task = asyncio.create_task(_do_call())
    register_active_task(query_record_id or "", task)

    try:
        response = await task
    except asyncio.CancelledError:
        await mark_query_cancelled(
            query_record_id=query_record_id,
            reason="Cancelled by user.",
        )
        # 499 isn't an HTTPException default, but FastAPI lets us raise it.
        raise HTTPException(status_code=499, detail="Request cancelled by user")
    except Exception as exc:
        logger.error(f"Claude API error: {exc}")
        await mark_query_failed(
            query_record_id=query_record_id,
            error=str(exc),
        )
        raise HTTPException(status_code=500, detail=f"Request failed: {str(exc)}")
    finally:
        unregister_active_task(query_record_id)

    # 4. Extract response text.
    content = ""
    for block in response.content:
        if hasattr(block, "text"):
            content += block.text

    metadata = {
        "model": "claude-3.5-sonnet-latest",
        "tokens_used": response.usage.input_tokens + response.usage.output_tokens,
        "input_tokens": response.usage.input_tokens,
        "output_tokens": response.usage.output_tokens,
        "stop_reason": response.stop_reason,
    }

    # 5. Persist the assistant message + mark the query record completed.
    await persist_assistant_message(
        chat_session_id=chat_session_id,
        user_id=user.id,
        client_id=client_id,
        content=content,
        ai_type=ai_type,
        format="text",
        idempotency_key=request.assistant_message_idempotency_key,
    )
    await mark_query_completed(
        query_record_id=query_record_id,
        result_markdown=content,
        tokens_used=metadata["tokens_used"],
    )

    return ChatResearchResponse(
        response=content,
        sources=None,
        metadata=metadata,
    )


# ─── Phase C2 — cancellation endpoint ────────────────────────────────
@router.post("/cancel/{query_record_id}")
async def cancel_chat_request(
    query_record_id: str,
    x_client_id: Optional[str] = Header(default=None, alias="X-Client-Id"),
    user: User = Depends(get_current_user),
):
    """
    Cancel an in-flight Claude chat request by its query_record_id.

    Effect:
      • Cancels the matching asyncio.Task on this replica (if any).
      • Marks the query_record `status="cancelled"` so OTHER replicas
        and the frontend can observe the state via the existing GET
        /api/query-records/{id} endpoint (defense-in-depth across the
        replica boundary).
    """
    cancelled = cancel_active_task(query_record_id)
    await mark_query_cancelled(
        query_record_id=query_record_id,
        reason="Cancelled by user.",
    )
    return {"cancelled": cancelled, "query_record_id": query_record_id}


# Research Endpoint
@router.post("/research", response_model=ChatResearchResponse)
async def chat_research(
    request: ChatResearchRequest,
    x_client_id: Optional[str] = Header(default=None, alias="X-Client-Id"),
    user: User = Depends(get_current_user),
):
    """
    Perform research using Claude API with optional web search.

    This endpoint uses Claude's extended thinking and research capabilities
    to provide comprehensive answers to research questions.
    """
    system_prompt = """You are an expert research assistant. Your role is to:

1. Conduct thorough research on the given topic
2. Analyze information from multiple perspectives
3. Synthesize findings into clear, comprehensive reports
4. Cite sources when making factual claims
5. Identify knowledge gaps and limitations

When responding:
- Provide a clear summary upfront
- Break down complex topics into understandable sections
- Include key findings and important details
- Be objective and balanced in your analysis
- Note any uncertainties or conflicting information
"""

    if request.use_research:
        system_prompt += """
You have access to current web information. Use this to:
- Find recent developments and data
- Verify facts and statistics
- Include multiple authoritative sources
- Compare different viewpoints
"""

    return await _run_claude_with_persistence(
        request=request,
        user=user,
        x_client_id=x_client_id,
        system_prompt=system_prompt,
        ai_type="claude-research",
    )


# Health check for Claude API
@router.get("/health")
async def health_check():
    """Check if Claude API is available and configured."""
    return {
        "claude_api_configured": anthropic_client is not None,
        "api_key_set": ANTHROPIC_API_KEY is not None,
        "status": "healthy" if anthropic_client else "not_configured"
    }


# Thinking Mode Endpoint
@router.post("/thinking", response_model=ChatResearchResponse)
async def chat_thinking(
    request: ChatResearchRequest,
    x_client_id: Optional[str] = Header(default=None, alias="X-Client-Id"),
    user: User = Depends(get_current_user),
):
    """
    Deep analytical thinking mode using Claude API.

    This endpoint uses Claude's extended thinking capabilities for:
    - Complex problem-solving
    - Analytical reasoning
    - Critical thinking
    - Hypothesis generation
    """
    system_prompt = """You are an expert analytical thinker. Your role is to:

1. Break down complex problems into manageable components
2. Analyze situations from multiple angles
3. Apply logical reasoning and critical thinking
4. Generate insights and novel perspectives
5. Challenge assumptions and identify biases

When responding:
- Think step-by-step through the problem
- Show your reasoning process
- Consider alternative viewpoints
- Identify key insights and implications
- Suggest actionable conclusions
"""
    return await _run_claude_with_persistence(
        request=request,
        user=user,
        x_client_id=x_client_id,
        system_prompt=system_prompt,
        ai_type="claude-thinking",
    )


# Coding Mode Endpoint
@router.post("/coding", response_model=ChatResearchResponse)
async def chat_coding(
    request: ChatResearchRequest,
    x_client_id: Optional[str] = Header(default=None, alias="X-Client-Id"),
    user: User = Depends(get_current_user),
):
    """
    Code generation and technical assistance using Claude API.

    This endpoint uses Claude for:
    - Code generation
    - Debugging assistance
    - Code review and optimization
    - Technical explanations
    """
    system_prompt = """You are an expert software engineer and coding assistant. Your role is to:

1. Write clean, efficient, and well-documented code
2. Debug issues and provide solutions
3. Review code for best practices and potential improvements
4. Explain technical concepts clearly
5. Suggest optimizations and alternative approaches

When responding:
- Provide working code examples
- Include helpful comments
- Follow language-specific best practices
- Explain your reasoning
- Consider edge cases and error handling
- Suggest testing approaches
"""
    return await _run_claude_with_persistence(
        request=request,
        user=user,
        x_client_id=x_client_id,
        system_prompt=system_prompt,
        ai_type="claude-coding",
    )


# Simple chat endpoint (no research mode)
@router.post("/message")
async def chat_message(prompt: str, max_tokens: int = 2048):
    """
    Simple chat endpoint for quick questions without research mode.
    """
    if not anthropic_client:
        raise HTTPException(
            status_code=503,
            detail="Claude API not configured"
        )

    try:
        response = await anthropic_client.messages.create(
            model="claude-sonnet-4-5-20250929",
            max_tokens=max_tokens,
            messages=[
                {
                    "role": "user",
                    "content": prompt
                }
            ]
        )

        content = ""
        for block in response.content:
            if hasattr(block, 'text'):
                content += block.text

        return {
            "response": content,
            "tokens_used": response.usage.input_tokens + response.usage.output_tokens
        }

    except Exception as e:
        logger.error(f"Claude API error: {e}")
        raise HTTPException(status_code=500, detail=str(e))