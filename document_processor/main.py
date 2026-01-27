"""
Main application entry point with FastAPI.
Provides REST API for document processing and monitoring.
"""

import asyncio
from contextlib import asynccontextmanager
from typing import List
import time
from fastapi import FastAPI, HTTPException, BackgroundTasks
from fastapi.responses import Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from fastapi.requests import Request
from fastapi.middleware.cors import CORSMiddleware
from .config.settings import settings
from .config.logging_config import logger
from .core.models import (
    SourceDocument,
    BatchProcessingRequest,
    BatchProcessingResponse,
    HealthStatus,
)
from .processing.pipeline import pipeline
from .infrastructure.monitoring import monitor
from .infrastructure.cache import cache_manager
from .infrastructure.storage import storage_manager
from .infrastructure.chat_store import chat_store

# Import API routers
try:
    from .api.chat_research_routes import router as chat_research_router
    CHAT_RESEARCH_AVAILABLE = True
except ImportError:
    CHAT_RESEARCH_AVAILABLE = False
    logger.warning("Chat research routes not available")

try:
    from .api.local_ai_routes_simple import router as local_ai_router, initialize_local_ai, cleanup_local_ai
    LOCAL_AI_AVAILABLE = True
except ImportError:
    LOCAL_AI_AVAILABLE = False
    logger.warning("Local AI routes not available")

from .api.chat_sessions_routes import router as chat_sessions_router
from .api.chat_folders_routes import router as chat_folders_router

# Crawling and Translation API routes
try:
    from .api.crawling_routes import router as crawling_router
    CRAWLING_AVAILABLE = True
except ImportError:
    CRAWLING_AVAILABLE = False
    logger.warning("Crawling routes not available")

try:
    from .api.translation_routes import router as translation_router
    TRANSLATION_AVAILABLE = True
except ImportError:
    TRANSLATION_AVAILABLE = False
    logger.warning("Translation routes not available")


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifespan manager."""
    # Startup
    logger.info("application_starting", service=settings.service_name)

    try:
        # Start pipeline
        await pipeline.start()

        # Ensure chat persistence indexes exist (MongoDB)
        try:
            await chat_store.ensure_indexes()
        except Exception as e:
            logger.warning("chat_store_indexes_failed", error=str(e))

        # Initialize Local AI if available
        if LOCAL_AI_AVAILABLE:
            import os
            ollama_url = os.getenv("OLLAMA_BASE_URL", "http://ollama:11434")
            ollama_model = os.getenv("OLLAMA_MODEL", "qwen2.5:7b")
            nllb_path = os.getenv("NLLB_MODEL_PATH")
            vector_path = os.getenv("LANCEDB_PATH", "/data/vectors")

            await initialize_local_ai(
                ollama_url=ollama_url,
                ollama_model=ollama_model,
                nllb_model_path=nllb_path,
                vector_db_path=vector_path
            )
            logger.info("local_ai_initialized")

        logger.info("application_started")
        yield
    finally:
        # Shutdown
        logger.info("application_stopping")

        # Cleanup Local AI if available
        if LOCAL_AI_AVAILABLE:
            await cleanup_local_ai()
            logger.info("local_ai_cleaned_up")

        await pipeline.stop()
        logger.info("application_stopped")


# Create FastAPI app
app = FastAPI(
    title="Document Processor",
    description="Production-ready multi-lingual document processing system",
    version="1.0.0",
    lifespan=lifespan,
)

# Add CORS middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Mount static files and templates
import os
web_ui_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), "web_ui")
app.mount("/static", StaticFiles(directory=os.path.join(web_ui_path, "static")), name="static")
templates = Jinja2Templates(directory=os.path.join(web_ui_path, "templates"))
app.state.static_version = os.getenv("STATIC_VERSION") or str(int(time.time()))


@app.get("/")
async def root(request: Request):
    """Serve the unified monochrome chat UI with Research, Thinking, and Coding modes."""
    return templates.TemplateResponse(
        "index.html",
        {"request": request, "static_version": app.state.static_version},
    )


@app.get("/api")
async def api_root():
    """API root endpoint."""
    return {
        "service": settings.service_name,
        "version": "1.0.0",
        "environment": settings.environment,
        "status": "running",
        "chat_research_available": CHAT_RESEARCH_AVAILABLE,
        "local_ai_available": LOCAL_AI_AVAILABLE,
        "crawling_available": CRAWLING_AVAILABLE,
        "translation_available": TRANSLATION_AVAILABLE,
    }


# Include API routers
if CHAT_RESEARCH_AVAILABLE:
    app.include_router(chat_research_router)
    logger.info("Chat research routes included")

if LOCAL_AI_AVAILABLE:
    app.include_router(local_ai_router)
    logger.info("Local AI routes included")

# Chat sessions persistence (MongoDB)
app.include_router(chat_sessions_router)
logger.info("Chat sessions routes included")

# Chat folders persistence (MongoDB)
app.include_router(chat_folders_router)
logger.info("Chat folders routes included")

# Crawling API routes
if CRAWLING_AVAILABLE:
    app.include_router(crawling_router)
    logger.info("Crawling routes included")

# Translation API routes
if TRANSLATION_AVAILABLE:
    app.include_router(translation_router)
    logger.info("Translation routes included")


@app.get("/health")
async def health_check():
    """Health check endpoint."""
    cache_healthy = await cache_manager.health_check()
    storage_health = await storage_manager.health_check()

    all_healthy = cache_healthy and all(storage_health.values())

    return HealthStatus(
        status="healthy" if all_healthy else "degraded",
        components={
            "cache": cache_healthy,
            **storage_health,
        },
    )


@app.get("/metrics")
async def metrics():
    """Prometheus metrics endpoint."""
    metrics_data = monitor.get_metrics()
    return Response(
        content=metrics_data,
        media_type=monitor.get_content_type(),
    )


@app.get("/stats")
async def get_stats():
    """Get processing statistics."""
    pipeline_metrics = pipeline.get_metrics()
    cache_stats = await cache_manager.get_stats()
    storage_stats = await storage_manager.get_statistics()

    return {
        "pipeline": pipeline_metrics.model_dump(),
        "cache": cache_stats,
        "storage": storage_stats,
    }


@app.post("/process")
async def process_documents(
    request: BatchProcessingRequest,
    background_tasks: BackgroundTasks = None,
):
    """
    Process batch of documents.

    Args:
        request: Batch processing request
        background_tasks: FastAPI background tasks

    Returns:
        Batch processing response
    """
    try:
        logger.info(
            "batch_request_received",
            count=len(request.sources),
            priority=request.priority,
            async_processing=request.async_processing,
        )

        # Validate sources
        if not request.sources:
            raise HTTPException(status_code=400, detail="No sources provided")

        if len(request.sources) > 10000:
            raise HTTPException(
                status_code=400,
                detail="Maximum batch size is 10000 documents",
            )

        # Process synchronously or asynchronously
        if request.async_processing:
            # Submit to background processing
            background_tasks.add_task(pipeline.process_batch, request.sources)

            response = BatchProcessingResponse(
                submitted=len(request.sources),
                estimated_completion_time_seconds=len(request.sources) * 2.0,  # Rough estimate
            )

            logger.info(
                "batch_submitted_async",
                batch_id=response.batch_id,
                count=len(request.sources),
            )

            return response

        else:
            # Process synchronously
            results = await pipeline.process_batch(request.sources)

            response = BatchProcessingResponse(
                submitted=len(request.sources),
                estimated_completion_time_seconds=0.0,
            )

            logger.info(
                "batch_processed_sync",
                batch_id=response.batch_id,
                count=len(request.sources),
                successful=len(results),
            )

            return response

    except Exception as e:
        logger.error("batch_processing_error", error=str(e), exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/process/single")
async def process_single_document(source: SourceDocument):
    """
    Process single document.

    Args:
        source: Source document

    Returns:
        Translated document
    """
    try:
        result = await pipeline.process_source(source)
        return result
    except Exception as e:
        logger.error(
            "single_document_processing_error",
            source_id=source.id,
            error=str(e),
        )
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/document/{document_id}")
async def get_document(document_id: str):
    """
    Get processed document by ID.

    Args:
        document_id: Document ID

    Returns:
        Translated document
    """
    document = await storage_manager.get_document(document_id)

    if not document:
        raise HTTPException(status_code=404, detail="Document not found")

    return document


@app.post("/reset-metrics")
async def reset_metrics():
    """Reset processing metrics."""
    pipeline.reset_metrics()
    return {"message": "Metrics reset successfully"}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "document_processor.main:app",
        host="0.0.0.0",
        port=8000,
        reload=settings.debug,
        log_level=settings.log_level.lower(),
    )
