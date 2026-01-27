"""
Chat-based Research API Routes
Handles both Claude API and Local AI research through a unified chat interface
"""

import asyncio
import logging
import os
from typing import Optional, List
from datetime import datetime

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field
from anthropic import AsyncAnthropic

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


class ChatResearchResponse(BaseModel):
    response: str = Field(..., description="Generated response")
    sources: Optional[List[dict]] = Field(None, description="Source citations if research was used")
    metadata: Optional[dict] = Field(None, description="Additional metadata")
    timestamp: str = Field(default_factory=lambda: datetime.utcnow().isoformat())


# Research Endpoint
@router.post("/research", response_model=ChatResearchResponse)
async def chat_research(request: ChatResearchRequest):
    """
    Perform research using Claude API with optional web search.

    This endpoint uses Claude's extended thinking and research capabilities
    to provide comprehensive answers to research questions.
    """
    if not anthropic_client:
        raise HTTPException(
            status_code=503,
            detail="Claude API not configured. Please set ANTHROPIC_API_KEY environment variable."
        )

    try:
        # Construct research prompt
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

        # Build conversation history for Claude
        messages = []
        for msg in request.history:
            role = "assistant" if msg.role == "assistant" else "user"
            messages.append({"role": role, "content": msg.content})

        # Current user prompt is always the final message
        messages.append({"role": "user", "content": request.prompt})

        # Call Claude API
        response = await anthropic_client.messages.create(
            model="claude-3.5-sonnet-latest",
            max_tokens=request.max_tokens,
            temperature=request.temperature,
            system=system_prompt,
            messages=messages,
        )

        # Extract response content
        content = ""
        for block in response.content:
            if hasattr(block, 'text'):
                content += block.text

        # Parse sources if available (basic implementation)
        # Note: Claude doesn't automatically provide structured sources
        # This would need to be extracted from the response text
        sources = []

        # Prepare metadata
        metadata = {
            "model": "claude-3.5-sonnet-latest",
            "tokens_used": response.usage.input_tokens + response.usage.output_tokens,
            "input_tokens": response.usage.input_tokens,
            "output_tokens": response.usage.output_tokens,
            "stop_reason": response.stop_reason
        }

        return ChatResearchResponse(
            response=content,
            sources=sources if sources else None,
            metadata=metadata
        )

    except Exception as e:
        logger.error(f"Claude API error: {e}")
        raise HTTPException(status_code=500, detail=f"Research failed: {str(e)}")


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
async def chat_thinking(request: ChatResearchRequest):
    """
    Deep analytical thinking mode using Claude API.

    This endpoint uses Claude's extended thinking capabilities for:
    - Complex problem-solving
    - Analytical reasoning
    - Critical thinking
    - Hypothesis generation
    """
    if not anthropic_client:
        raise HTTPException(
            status_code=503,
            detail="Claude API not configured. Please set ANTHROPIC_API_KEY environment variable."
        )

    try:
        # Construct thinking prompt
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

        # Build conversation history for Claude
        messages = []
        for msg in request.history:
            role = "assistant" if msg.role == "assistant" else "user"
            messages.append({"role": role, "content": msg.content})

        messages.append({"role": "user", "content": request.prompt})

        # Call Claude API
        response = await anthropic_client.messages.create(
            model="claude-3.5-sonnet-latest",
            max_tokens=request.max_tokens,
            temperature=request.temperature,
            system=system_prompt,
            messages=messages,
        )

        # Extract response content
        content = ""
        for block in response.content:
            if hasattr(block, 'text'):
                content += block.text

        # Prepare metadata
        metadata = {
            "model": "claude-3.5-sonnet-latest",
            "mode": "thinking",
            "tokens_used": response.usage.input_tokens + response.usage.output_tokens,
            "input_tokens": response.usage.input_tokens,
            "output_tokens": response.usage.output_tokens,
            "stop_reason": response.stop_reason
        }

        return ChatResearchResponse(
            response=content,
            sources=None,
            metadata=metadata
        )

    except Exception as e:
        logger.error(f"Claude API error (thinking): {e}")
        raise HTTPException(status_code=500, detail=f"Thinking mode failed: {str(e)}")


# Coding Mode Endpoint
@router.post("/coding", response_model=ChatResearchResponse)
async def chat_coding(request: ChatResearchRequest):
    """
    Code generation and technical assistance using Claude API.

    This endpoint uses Claude for:
    - Code generation
    - Debugging assistance
    - Code review and optimization
    - Technical explanations
    """
    if not anthropic_client:
        raise HTTPException(
            status_code=503,
            detail="Claude API not configured. Please set ANTHROPIC_API_KEY environment variable."
        )

    try:
        # Construct coding prompt
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

        # Build conversation history for Claude
        messages = []
        for msg in request.history:
            role = "assistant" if msg.role == "assistant" else "user"
            messages.append({"role": role, "content": msg.content})

        messages.append({"role": "user", "content": request.prompt})

        # Call Claude API
        response = await anthropic_client.messages.create(
            model="claude-3.5-sonnet-latest",
            max_tokens=request.max_tokens,
            temperature=request.temperature,
            system=system_prompt,
            messages=messages,
        )

        # Extract response content
        content = ""
        for block in response.content:
            if hasattr(block, 'text'):
                content += block.text

        # Prepare metadata
        metadata = {
            "model": "claude-3.5-sonnet-latest",
            "mode": "coding",
            "tokens_used": response.usage.input_tokens + response.usage.output_tokens,
            "input_tokens": response.usage.input_tokens,
            "output_tokens": response.usage.output_tokens,
            "stop_reason": response.stop_reason
        }

        return ChatResearchResponse(
            response=content,
            sources=None,
            metadata=metadata
        )

    except Exception as e:
        logger.error(f"Claude API error (coding): {e}")
        raise HTTPException(status_code=500, detail=f"Coding mode failed: {str(e)}")


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