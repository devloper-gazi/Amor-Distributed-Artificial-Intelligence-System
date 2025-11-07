"""
Main application entry point with FastAPI.
Provides REST API for document processing and monitoring.
"""

import asyncio
from contextlib import asynccontextmanager
from typing import List
from fastapi import FastAPI, HTTPException, BackgroundTasks
from fastapi.responses import Response
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


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifespan manager."""
    # Startup
    logger.info("application_starting", service=settings.service_name)

    try:
        # Start pipeline
        await pipeline.start()
        logger.info("application_started")
        yield
    finally:
        # Shutdown
        logger.info("application_stopping")
        await pipeline.stop()
        logger.info("application_stopped")


# Create FastAPI app
app = FastAPI(
    title="Document Processor",
    description="Production-ready multi-lingual document processing system",
    version="1.0.0",
    lifespan=lifespan,
)


@app.get("/")
async def root():
    """Root endpoint."""
    return {
        "service": settings.service_name,
        "version": "1.0.0",
        "environment": settings.environment,
        "status": "running",
    }


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
