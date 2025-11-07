"""
Monitoring and observability with Prometheus metrics and structured logging.
Provides comprehensive metrics for tracking system performance.
"""

import time
from contextlib import asynccontextmanager
from typing import Optional
from prometheus_client import (
    Counter,
    Histogram,
    Gauge,
    Info,
    CollectorRegistry,
    generate_latest,
    CONTENT_TYPE_LATEST,
)
from ..config.settings import settings
from ..config.logging_config import logger


# Create custom registry
REGISTRY = CollectorRegistry(auto_describe=True)

# Document processing metrics
DOCUMENTS_PROCESSED = Counter(
    "documents_processed_total",
    "Total documents processed",
    ["source_type", "status"],
    registry=REGISTRY,
)

DOCUMENTS_IN_PROGRESS = Gauge(
    "documents_in_progress",
    "Documents currently being processed",
    ["source_type"],
    registry=REGISTRY,
)

PROCESSING_DURATION = Histogram(
    "processing_duration_seconds",
    "Document processing duration in seconds",
    ["source_type"],
    buckets=[0.1, 0.5, 1.0, 2.0, 5.0, 10.0, 30.0, 60.0, 120.0, 300.0],
    registry=REGISTRY,
)

# Translation metrics
TRANSLATION_REQUESTS = Counter(
    "translation_requests_total",
    "Total translation requests",
    ["provider", "source_lang", "status"],
    registry=REGISTRY,
)

TRANSLATION_DURATION = Histogram(
    "translation_duration_seconds",
    "Translation duration in seconds",
    ["provider"],
    buckets=[0.1, 0.5, 1.0, 2.0, 5.0, 10.0, 30.0],
    registry=REGISTRY,
)

TRANSLATION_CHARACTERS = Counter(
    "translation_characters_total",
    "Total characters translated",
    ["provider"],
    registry=REGISTRY,
)

TRANSLATION_COST = Counter(
    "translation_cost_usd",
    "Estimated translation cost in USD",
    ["provider"],
    registry=REGISTRY,
)

# Language detection metrics
LANGUAGE_DETECTIONS = Counter(
    "language_detections_total",
    "Total language detections",
    ["detected_language"],
    registry=REGISTRY,
)

LANGUAGE_DETECTION_CONFIDENCE = Histogram(
    "language_detection_confidence",
    "Language detection confidence scores",
    buckets=[0.5, 0.6, 0.7, 0.8, 0.9, 0.95, 0.99, 1.0],
    registry=REGISTRY,
)

# Cache metrics
CACHE_OPERATIONS = Counter(
    "cache_operations_total",
    "Cache operations",
    ["operation", "result"],
    registry=REGISTRY,
)

CACHE_HIT_RATE = Gauge(
    "cache_hit_rate",
    "Cache hit rate percentage",
    registry=REGISTRY,
)

# Queue metrics
QUEUE_DEPTH = Gauge(
    "queue_depth",
    "Current queue depth",
    ["queue_name"],
    registry=REGISTRY,
)

QUEUE_MESSAGES_PUBLISHED = Counter(
    "queue_messages_published_total",
    "Messages published to queue",
    ["queue_name"],
    registry=REGISTRY,
)

QUEUE_MESSAGES_CONSUMED = Counter(
    "queue_messages_consumed_total",
    "Messages consumed from queue",
    ["queue_name", "status"],
    registry=REGISTRY,
)

# Worker metrics
ACTIVE_WORKERS = Gauge(
    "active_workers",
    "Number of active workers",
    registry=REGISTRY,
)

WORKER_TASKS = Gauge(
    "worker_tasks",
    "Number of tasks per worker",
    ["worker_id"],
    registry=REGISTRY,
)

# Error metrics
ERRORS_TOTAL = Counter(
    "errors_total",
    "Total errors",
    ["error_type", "component"],
    registry=REGISTRY,
)

DLQ_MESSAGES = Gauge(
    "dlq_messages",
    "Messages in dead letter queue",
    registry=REGISTRY,
)

# Circuit breaker metrics
CIRCUIT_BREAKER_STATE = Gauge(
    "circuit_breaker_state",
    "Circuit breaker state (0=closed, 1=half-open, 2=open)",
    ["service"],
    registry=REGISTRY,
)

CIRCUIT_BREAKER_FAILURES = Counter(
    "circuit_breaker_failures_total",
    "Circuit breaker failures",
    ["service"],
    registry=REGISTRY,
)

# Rate limiter metrics
RATE_LIMIT_DELAYS = Histogram(
    "rate_limit_delay_seconds",
    "Rate limit delay duration",
    ["service"],
    buckets=[0.1, 0.5, 1.0, 2.0, 5.0, 10.0, 30.0],
    registry=REGISTRY,
)

# System metrics
SYSTEM_INFO = Info(
    "system",
    "System information",
    registry=REGISTRY,
)

# Source processor metrics
SOURCE_EXTRACTIONS = Counter(
    "source_extractions_total",
    "Source content extractions",
    ["source_type", "status"],
    registry=REGISTRY,
)

SOURCE_EXTRACTION_DURATION = Histogram(
    "source_extraction_duration_seconds",
    "Source extraction duration",
    ["source_type"],
    buckets=[0.5, 1.0, 2.0, 5.0, 10.0, 30.0, 60.0],
    registry=REGISTRY,
)


class Monitor:
    """
    Monitoring and metrics manager.

    Provides methods for recording metrics and tracking system performance.
    """

    def __init__(self):
        """Initialize monitor."""
        self.logger = logger

        # Set system info
        SYSTEM_INFO.info({
            "service": settings.service_name,
            "environment": settings.environment,
            "version": "1.0.0",  # Could be loaded from package
        })

    # Document processing metrics
    def record_document_processed(self, source_type: str, status: str):
        """Record document processed."""
        DOCUMENTS_PROCESSED.labels(source_type=source_type, status=status).inc()

    def record_document_in_progress(self, source_type: str, delta: int = 1):
        """Update documents in progress counter."""
        if delta > 0:
            DOCUMENTS_IN_PROGRESS.labels(source_type=source_type).inc(delta)
        else:
            DOCUMENTS_IN_PROGRESS.labels(source_type=source_type).dec(abs(delta))

    @asynccontextmanager
    async def track_processing_duration(self, source_type: str):
        """Context manager to track processing duration."""
        start_time = time.time()
        try:
            yield
        finally:
            duration = time.time() - start_time
            PROCESSING_DURATION.labels(source_type=source_type).observe(duration)

    # Translation metrics
    def record_translation(
        self,
        provider: str,
        source_lang: str,
        status: str,
        characters: int = 0,
        cost: float = 0.0,
    ):
        """Record translation request."""
        TRANSLATION_REQUESTS.labels(
            provider=provider,
            source_lang=source_lang,
            status=status
        ).inc()

        if characters > 0:
            TRANSLATION_CHARACTERS.labels(provider=provider).inc(characters)

        if cost > 0:
            TRANSLATION_COST.labels(provider=provider).inc(cost)

    @asynccontextmanager
    async def track_translation_duration(self, provider: str):
        """Context manager to track translation duration."""
        start_time = time.time()
        try:
            yield
        finally:
            duration = time.time() - start_time
            TRANSLATION_DURATION.labels(provider=provider).observe(duration)

    # Language detection metrics
    def record_language_detection(self, language: str, confidence: float):
        """Record language detection."""
        LANGUAGE_DETECTIONS.labels(detected_language=language).inc()
        LANGUAGE_DETECTION_CONFIDENCE.observe(confidence)

    # Cache metrics
    def record_cache_operation(self, operation: str, result: str):
        """Record cache operation."""
        CACHE_OPERATIONS.labels(operation=operation, result=result).inc()

    def update_cache_hit_rate(self, hit_rate: float):
        """Update cache hit rate gauge."""
        CACHE_HIT_RATE.set(hit_rate)

    # Queue metrics
    def update_queue_depth(self, queue_name: str, depth: int):
        """Update queue depth gauge."""
        QUEUE_DEPTH.labels(queue_name=queue_name).set(depth)

    def record_queue_publish(self, queue_name: str):
        """Record message published to queue."""
        QUEUE_MESSAGES_PUBLISHED.labels(queue_name=queue_name).inc()

    def record_queue_consume(self, queue_name: str, status: str):
        """Record message consumed from queue."""
        QUEUE_MESSAGES_CONSUMED.labels(queue_name=queue_name, status=status).inc()

    # Worker metrics
    def update_active_workers(self, count: int):
        """Update active workers gauge."""
        ACTIVE_WORKERS.set(count)

    def update_worker_tasks(self, worker_id: str, count: int):
        """Update worker tasks gauge."""
        WORKER_TASKS.labels(worker_id=worker_id).set(count)

    # Error metrics
    def record_error(self, error_type: str, component: str):
        """Record error."""
        ERRORS_TOTAL.labels(error_type=error_type, component=component).inc()

    def update_dlq_messages(self, count: int):
        """Update DLQ messages gauge."""
        DLQ_MESSAGES.set(count)

    # Circuit breaker metrics
    def update_circuit_breaker_state(self, service: str, state: str):
        """Update circuit breaker state."""
        state_value = {"closed": 0, "half_open": 1, "open": 2}.get(state, 0)
        CIRCUIT_BREAKER_STATE.labels(service=service).set(state_value)

    def record_circuit_breaker_failure(self, service: str):
        """Record circuit breaker failure."""
        CIRCUIT_BREAKER_FAILURES.labels(service=service).inc()

    # Rate limiter metrics
    def record_rate_limit_delay(self, service: str, delay: float):
        """Record rate limit delay."""
        RATE_LIMIT_DELAYS.labels(service=service).observe(delay)

    # Source processor metrics
    def record_source_extraction(self, source_type: str, status: str):
        """Record source extraction."""
        SOURCE_EXTRACTIONS.labels(source_type=source_type, status=status).inc()

    @asynccontextmanager
    async def track_extraction_duration(self, source_type: str):
        """Context manager to track extraction duration."""
        start_time = time.time()
        try:
            yield
        finally:
            duration = time.time() - start_time
            SOURCE_EXTRACTION_DURATION.labels(source_type=source_type).observe(duration)

    def get_metrics(self) -> bytes:
        """
        Get Prometheus metrics.

        Returns:
            Metrics in Prometheus text format
        """
        return generate_latest(REGISTRY)

    def get_content_type(self) -> str:
        """
        Get metrics content type.

        Returns:
            Content type for Prometheus metrics
        """
        return CONTENT_TYPE_LATEST


# Global monitor instance
monitor = Monitor()
