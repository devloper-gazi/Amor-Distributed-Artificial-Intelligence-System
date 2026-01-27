"""
Local AI Research API Routes
FastAPI endpoints for autonomous research with CrewAI and Ollama
"""

import asyncio
import logging
from typing import List, Optional, Dict, Any
from datetime import datetime
from uuid import uuid4

from fastapi import APIRouter, HTTPException, BackgroundTasks
from pydantic import BaseModel, Field

from local_ai import (
    OllamaClient,
    NLLBTranslator,
    AutonomousScraper,
    LanceDBVectorStore,
)
from local_ai.agents import ResearchCrew, ResearchOutput

logger = logging.getLogger(__name__)

# Create router
router = APIRouter(prefix="/api/local-ai", tags=["local-ai"])

# Global instances (initialized on startup)
ollama_client: Optional[OllamaClient] = None
translator: Optional[NLLBTranslator] = None
scraper: Optional[AutonomousScraper] = None
vector_store: Optional[LanceDBVectorStore] = None
research_crew: Optional[ResearchCrew] = None

# Active research sessions
research_sessions: Dict[str, Dict[str, Any]] = {}


# Request/Response Models
class ResearchRequest(BaseModel):
    topic: str = Field(..., min_length=1, description="Research topic or question")
    depth: str = Field("standard", description="Research depth: quick, standard, or deep")
    sources: Optional[List[str]] = Field(None, description="Optional list of source URLs")
    use_translation: bool = Field(True, description="Enable multilingual translation")
    save_to_knowledge: bool = Field(True, description="Save results to vector store")


class ResearchStatusResponse(BaseModel):
    session_id: str
    status: str
    progress: int
    current_agent: Optional[str] = None
    current_task: Optional[str] = None
    timeline: List[Dict[str, Any]] = []


class VectorSearchRequest(BaseModel):
    query: str = Field(..., min_length=1, description="Search query")
    limit: int = Field(5, ge=1, le=50, description="Maximum results")


# Startup and Shutdown
async def initialize_local_ai(
    ollama_url: str = "http://ollama:11434",
    ollama_model: str = "qwen2.5:7b",
    nllb_model_path: Optional[str] = None,
    vector_db_path: str = "/data/vectors",
):
    """Initialize local AI components."""
    global ollama_client, translator, scraper, vector_store, research_crew

    try:
        logger.info("Initializing local AI system...")

        # Initialize Ollama client
        ollama_client = OllamaClient(
            base_url=ollama_url,
            model=ollama_model,
        )
        logger.info("Ollama client initialized")

        # Initialize translator if path provided
        if nllb_model_path:
            try:
                translator = NLLBTranslator(
                    model_path=nllb_model_path,
                    device="cuda",
                    compute_type="int8_float16",
                )
                logger.info("NLLB translator initialized")
            except Exception as e:
                logger.warning(f"Failed to initialize translator: {e}")
                translator = None

        # Initialize web scraper
        scraper = AutonomousScraper(
            delay_between_requests=2.0,
            headless=True,
        )
        logger.info("Web scraper initialized")

        # Initialize vector store
        vector_store = LanceDBVectorStore(
            db_path=vector_db_path,
            device="cpu",  # Use CPU to save VRAM
        )
        logger.info("Vector store initialized")

        # Initialize research crew
        research_crew = ResearchCrew(
            ollama_base_url=ollama_url,
            ollama_model=ollama_model,
            translator=translator,
            scraper=scraper,
        )
        logger.info("Research crew initialized")

        logger.info("Local AI system ready")

    except Exception as e:
        logger.error(f"Failed to initialize local AI system: {e}")
        raise


async def cleanup_local_ai():
    """Cleanup local AI resources."""
    global scraper

    if scraper:
        await scraper.close()

    logger.info("Local AI system cleaned up")


# Health Check
@router.get("/health")
async def health_check():
    """Check health of local AI components."""
    try:
        health_status = {
            "status": "healthy",
            "timestamp": datetime.utcnow().isoformat(),
        }

        # Check Ollama
        if ollama_client:
            try:
                is_healthy = await ollama_client.health()
                health_status["ollama_status"] = "healthy" if is_healthy else "unhealthy"

                # Get model info
                models = await ollama_client.list_models()
                health_status["model_loaded"] = any(
                    m.get("name") == ollama_client.model for m in models
                )

                # Estimate VRAM usage (approximate)
                health_status["vram_usage_mb"] = 4500 if health_status["model_loaded"] else 0

            except Exception as e:
                health_status["ollama_status"] = "unhealthy"
                health_status["ollama_error"] = str(e)
        else:
            health_status["ollama_status"] = "not initialized"

        # Check other components
        health_status["translator_available"] = translator is not None
        health_status["scraper_available"] = scraper is not None
        health_status["vector_store_available"] = vector_store is not None
        health_status["research_crew_available"] = research_crew is not None

        # Overall status
        if health_status["ollama_status"] != "healthy":
            health_status["status"] = "degraded"

        return health_status

    except Exception as e:
        logger.error(f"Health check failed: {e}")
        return {
            "status": "unhealthy",
            "error": str(e),
            "timestamp": datetime.utcnow().isoformat(),
        }


# Research Endpoints
@router.post("/research")
async def start_research(
    request: ResearchRequest,
    background_tasks: BackgroundTasks,
):
    """Start autonomous research on a topic."""
    if not research_crew:
        raise HTTPException(status_code=503, detail="Research crew not initialized")

    try:
        # Create session
        session_id = str(uuid4())

        # Initialize session tracking
        research_sessions[session_id] = {
            "session_id": session_id,
            "topic": request.topic,
            "depth": request.depth,
            "status": "started",
            "progress": 0,
            "current_agent": None,
            "current_task": "Initializing research",
            "timeline": [
                {
                    "timestamp": datetime.utcnow().isoformat(),
                    "agent": "System",
                    "description": "Research session started",
                    "status": "completed",
                }
            ],
            "started_at": datetime.utcnow().isoformat(),
        }

        # Start research in background
        background_tasks.add_task(
            execute_research,
            session_id,
            request,
        )

        return {
            "success": True,
            "session_id": session_id,
            "message": "Research started",
        }

    except Exception as e:
        logger.error(f"Failed to start research: {e}")
        raise HTTPException(status_code=500, detail=str(e))


async def execute_research(session_id: str, request: ResearchRequest):
    """Execute research workflow in background."""
    try:
        session = research_sessions[session_id]

        # Update status
        session["status"] = "in_progress"
        session["progress"] = 10
        session["current_agent"] = "Research Specialist"
        session["current_task"] = "Gathering information"

        # Execute research
        result: ResearchOutput = await research_crew.research(
            topic=request.topic,
            sources=request.sources,
            depth=request.depth,
        )

        # Update progress
        session["progress"] = 80
        session["current_agent"] = "Writer"
        session["current_task"] = "Generating report"

        # Save to vector store if requested
        if request.save_to_knowledge and vector_store:
            await vector_store.add_document(
                text=result.analysis,
                document_id=session_id,
                title=f"Research: {request.topic}",
                source_url=None,
            )

        # Mark complete
        session["status"] = "completed"
        session["progress"] = 100
        session["current_agent"] = None
        session["current_task"] = None
        session["result"] = {
            "summary": result.summary,
            "findings": result.findings,
            "analysis": result.analysis,
            "sources": [{"url": s, "title": s} for s in result.sources],
            "confidence": result.confidence,
        }
        session["completed_at"] = datetime.utcnow().isoformat()

        logger.info(f"Research completed for session {session_id}")

    except Exception as e:
        logger.error(f"Research failed for session {session_id}: {e}")
        session["status"] = "failed"
        session["error"] = str(e)


@router.get("/research/{session_id}/status")
async def get_research_status(session_id: str):
    """Get research session status."""
    if session_id not in research_sessions:
        raise HTTPException(status_code=404, detail="Session not found")

    session = research_sessions[session_id]

    response = ResearchStatusResponse(
        session_id=session_id,
        status=session["status"],
        progress=session["progress"],
        current_agent=session.get("current_agent"),
        current_task=session.get("current_task"),
        timeline=session.get("timeline", []),
    )

    # Include result if completed
    if session["status"] == "completed" and "result" in session:
        response = {**response.dict(), **session["result"]}

    return response


@router.get("/research/{session_id}")
async def get_research_result(session_id: str):
    """Get completed research result."""
    if session_id not in research_sessions:
        raise HTTPException(status_code=404, detail="Session not found")

    session = research_sessions[session_id]

    if session["status"] != "completed":
        raise HTTPException(status_code=400, detail="Research not completed yet")

    return session["result"]


@router.post("/research/{session_id}/stop")
async def stop_research(session_id: str):
    """Stop active research session."""
    if session_id not in research_sessions:
        raise HTTPException(status_code=404, detail="Session not found")

    session = research_sessions[session_id]

    if session["status"] in ["completed", "failed"]:
        raise HTTPException(status_code=400, detail="Research already finished")

    session["status"] = "stopped"
    session["stopped_at"] = datetime.utcnow().isoformat()

    return {"success": True, "message": "Research stopped"}


@router.get("/research/history")
async def get_research_history(limit: int = 10):
    """Get research history."""
    history = []

    for session_id, session in sorted(
        research_sessions.items(),
        key=lambda x: x[1].get("started_at", ""),
        reverse=True,
    )[:limit]:
        history.append({
            "session_id": session_id,
            "topic": session["topic"],
            "depth": session["depth"],
            "status": session["status"],
            "timestamp": session["started_at"],
            "sources_count": len(session.get("result", {}).get("sources", [])),
        })

    return history


# Vector Search Endpoints
@router.post("/vector-search")
async def vector_search(request: VectorSearchRequest):
    """Perform semantic vector search."""
    if not vector_store:
        raise HTTPException(status_code=503, detail="Vector store not initialized")

    try:
        results = await vector_store.search(
            query=request.query,
            limit=request.limit,
        )

        return results

    except Exception as e:
        logger.error(f"Vector search failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/vector-search/stats")
async def get_vector_stats():
    """Get vector store statistics."""
    if not vector_store:
        raise HTTPException(status_code=503, detail="Vector store not initialized")

    try:
        stats = await vector_store.get_stats()
        return stats

    except Exception as e:
        logger.error(f"Failed to get vector stats: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# Ollama Endpoints
@router.post("/ollama/generate")
async def generate_text(
    prompt: str,
    system: Optional[str] = None,
    max_tokens: Optional[int] = 2048,
):
    """Generate text using Ollama."""
    if not ollama_client:
        raise HTTPException(status_code=503, detail="Ollama client not initialized")

    try:
        response = await ollama_client.generate(
            prompt=prompt,
            system=system,
            max_tokens=max_tokens,
        )

        return {"response": response}

    except Exception as e:
        logger.error(f"Generation failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/ollama/models")
async def list_models():
    """List available Ollama models."""
    if not ollama_client:
        raise HTTPException(status_code=503, detail="Ollama client not initialized")

    try:
        models = await ollama_client.list_models()
        return {"models": models}

    except Exception as e:
        logger.error(f"Failed to list models: {e}")
        raise HTTPException(status_code=500, detail=str(e))