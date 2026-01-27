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

# ============================================================================
# CRAWLING METRICS
# ============================================================================

CRAWL_PAGES_TOTAL = Counter(
    "crawl_pages_total",
    "Total pages crawled",
    ["domain", "status"],
    registry=REGISTRY,
)

CRAWL_PAGES_IN_PROGRESS = Gauge(
    "crawl_pages_in_progress",
    "Pages currently being crawled",
    registry=REGISTRY,
)

CRAWL_RESPONSE_TIME = Histogram(
    "crawl_response_time_seconds",
    "HTTP response time for crawled pages",
    ["domain"],
    buckets=[0.1, 0.25, 0.5, 1.0, 2.0, 5.0, 10.0, 30.0],
    registry=REGISTRY,
)

CRAWL_TIMEOUT_RATE = Gauge(
    "crawl_timeout_rate",
    "Percentage of requests that timeout",
    registry=REGISTRY,
)

CRAWL_BYTES_DOWNLOADED = Counter(
    "crawl_bytes_downloaded_total",
    "Total bytes downloaded",
    registry=REGISTRY,
)

CRAWL_QUEUE_DEPTH = Gauge(
    "crawl_queue_depth",
    "Number of URLs in the crawl queue",
    registry=REGISTRY,
)

CRAWL_ACTIVE_DOMAINS = Gauge(
    "crawl_active_domains",
    "Number of active domains being crawled",
    registry=REGISTRY,
)

CRAWL_REQUESTS_PER_SECOND = Gauge(
    "crawl_requests_per_second",
    "Current crawl rate in requests per second",
    registry=REGISTRY,
)

CRAWL_DOMAIN_DELAYS = Histogram(
    "crawl_domain_delay_seconds",
    "Per-domain crawl delays",
    buckets=[0.5, 1.0, 2.0, 5.0, 10.0, 30.0],
    registry=REGISTRY,
)

CRAWL_CIRCUIT_BREAKER_TRIPS = Counter(
    "crawl_circuit_breaker_trips_total",
    "Number of circuit breaker trips",
    ["domain"],
    registry=REGISTRY,
)

CRAWL_RETRIES = Counter(
    "crawl_retries_total",
    "Total number of crawl retries",
    ["reason"],
    registry=REGISTRY,
)

CRAWL_DEDUP_SKIPPED = Counter(
    "crawl_dedup_skipped_total",
    "URLs skipped due to deduplication",
    registry=REGISTRY,
)

# ============================================================================
# TRANSLATION PIPELINE METRICS
# ============================================================================

TRANSLATION_THROUGHPUT = Gauge(
    "translation_throughput_tokens_per_second",
    "Translation throughput in tokens per second",
    registry=REGISTRY,
)

TRANSLATION_BATCH_SIZE = Histogram(
    "translation_batch_size",
    "Size of translation batches",
    buckets=[1, 2, 4, 8, 16, 32, 64],
    registry=REGISTRY,
)

TRANSLATION_QUEUE_DEPTH = Gauge(
    "translation_queue_depth",
    "Number of texts waiting for translation",
    registry=REGISTRY,
)

TRANSLATION_CACHE_HITS = Counter(
    "translation_cache_hits_total",
    "Translation cache hits",
    registry=REGISTRY,
)

TRANSLATION_CACHE_MISSES = Counter(
    "translation_cache_misses_total",
    "Translation cache misses",
    registry=REGISTRY,
)

TRANSLATION_LANGUAGE_PAIRS = Counter(
    "translation_language_pairs_total",
    "Translations by language pair",
    ["source_lang", "target_lang"],
    registry=REGISTRY,
)

TRANSLATION_QUALITY_SCORE = Histogram(
    "translation_quality_score",
    "Translation quality/confidence scores",
    buckets=[0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9, 1.0],
    registry=REGISTRY,
)

# ============================================================================
# RAG METRICS
# ============================================================================

RAG_QUERY_LATENCY = Histogram(
    "rag_query_latency_seconds",
    "RAG query latency",
    buckets=[0.1, 0.25, 0.5, 1.0, 2.0, 5.0, 10.0],
    registry=REGISTRY,
)

RAG_DOCUMENTS_RETRIEVED = Histogram(
    "rag_documents_retrieved",
    "Number of documents retrieved per query",
    buckets=[1, 2, 3, 5, 10, 15, 20],
    registry=REGISTRY,
)

RAG_RELEVANCE_SCORES = Histogram(
    "rag_relevance_scores",
    "Document relevance scores",
    buckets=[0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9, 1.0],
    registry=REGISTRY,
)

RAG_TOTAL_DOCUMENTS = Gauge(
    "rag_total_documents",
    "Total documents in RAG index",
    registry=REGISTRY,
)

RAG_TOTAL_CHUNKS = Gauge(
    "rag_total_chunks",
    "Total chunks in RAG index",
    registry=REGISTRY,
)

# ============================================================================
# PIPELINE METRICS
# ============================================================================

PIPELINE_STAGE_DURATION = Histogram(
    "pipeline_stage_duration_seconds",
    "Duration of each pipeline stage",
    ["stage"],
    buckets=[0.1, 0.5, 1.0, 2.0, 5.0, 10.0, 30.0, 60.0],
    registry=REGISTRY,
)

PIPELINE_DOCUMENTS_IN_FLIGHT = Gauge(
    "pipeline_documents_in_flight",
    "Documents currently in the pipeline",
    registry=REGISTRY,
)

PIPELINE_STAGE_QUEUE_DEPTH = Gauge(
    "pipeline_stage_queue_depth",
    "Queue depth for each pipeline stage",
    ["stage"],
    registry=REGISTRY,
)

# ============================================================================
# STORAGE METRICS
# ============================================================================

STORAGE_INSERT_LATENCY = Histogram(
    "storage_insert_latency_seconds",
    "Storage insert latency in seconds",
    ["storage_type"],  # postgresql, mongodb, lancedb
    buckets=[0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.0],
    registry=REGISTRY,
)

STORAGE_QUERY_LATENCY = Histogram(
    "storage_query_latency_seconds",
    "Storage query latency in seconds",
    ["storage_type", "query_type"],
    buckets=[0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.0, 5.0],
    registry=REGISTRY,
)

STORAGE_DOCUMENTS_TOTAL = Gauge(
    "storage_documents_total",
    "Total documents in storage",
    ["storage_type"],
    registry=REGISTRY,
)

STORAGE_SIZE_BYTES = Gauge(
    "storage_size_bytes",
    "Storage size in bytes",
    ["storage_type"],
    registry=REGISTRY,
)

STORAGE_CONNECTIONS_ACTIVE = Gauge(
    "storage_connections_active",
    "Active storage connections",
    ["storage_type"],
    registry=REGISTRY,
)

STORAGE_ERRORS = Counter(
    "storage_errors_total",
    "Storage operation errors",
    ["storage_type", "operation", "error_type"],
    registry=REGISTRY,
)

# ============================================================================
# TRANSLATION QUALITY METRICS
# ============================================================================

TRANSLATION_BLEU_SCORE = Histogram(
    "translation_bleu_score",
    "BLEU score for translation quality (when reference available)",
    buckets=[0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9, 1.0],
    registry=REGISTRY,
)

TRANSLATION_MODEL_MEMORY_BYTES = Gauge(
    "translation_model_memory_bytes",
    "Memory used by translation model",
    registry=REGISTRY,
)

TRANSLATION_GPU_UTILIZATION = Gauge(
    "translation_gpu_utilization_percent",
    "GPU utilization for translation (if available)",
    registry=REGISTRY,
)

# ============================================================================
# SYSTEM HEALTH METRICS
# ============================================================================

SYSTEM_CPU_USAGE = Gauge(
    "system_cpu_usage_percent",
    "System CPU usage percentage",
    registry=REGISTRY,
)

SYSTEM_MEMORY_USAGE = Gauge(
    "system_memory_usage_percent",
    "System memory usage percentage",
    registry=REGISTRY,
)

SYSTEM_DISK_USAGE = Gauge(
    "system_disk_usage_percent",
    "System disk usage percentage",
    ["mount_point"],
    registry=REGISTRY,
)

# ============================================================================
# ALERTING THRESHOLDS (for reference)
# ============================================================================

ALERT_CRAWL_TIMEOUT_RATE_THRESHOLD = 1.0  # Alert if timeout rate > 1%
ALERT_STORAGE_LATENCY_THRESHOLD = 0.05  # Alert if storage latency > 50ms
ALERT_RAG_LATENCY_THRESHOLD = 2.0  # Alert if RAG latency > 2 seconds
ALERT_QUEUE_DEPTH_THRESHOLD = 10000  # Alert if queue depth > 10000


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

    # ========================================================================
    # CRAWLING METRICS
    # ========================================================================
    
    def record_crawl_page(self, domain: str, status: str):
        """Record a crawled page."""
        CRAWL_PAGES_TOTAL.labels(domain=domain, status=status).inc()
    
    def update_crawl_in_progress(self, count: int):
        """Update pages in progress."""
        CRAWL_PAGES_IN_PROGRESS.set(count)
    
    def record_crawl_response_time(self, domain: str, response_time: float):
        """Record HTTP response time."""
        CRAWL_RESPONSE_TIME.labels(domain=domain).observe(response_time)
    
    def update_crawl_timeout_rate(self, rate: float):
        """Update timeout rate (0-1)."""
        CRAWL_TIMEOUT_RATE.set(rate * 100)  # Convert to percentage
    
    def record_crawl_bytes(self, bytes_count: int):
        """Record bytes downloaded."""
        CRAWL_BYTES_DOWNLOADED.inc(bytes_count)
    
    def update_crawl_queue_depth(self, depth: int):
        """Update crawl queue depth."""
        CRAWL_QUEUE_DEPTH.set(depth)
    
    def update_active_domains(self, count: int):
        """Update active domains count."""
        CRAWL_ACTIVE_DOMAINS.set(count)
    
    def update_crawl_rate(self, rps: float):
        """Update requests per second."""
        CRAWL_REQUESTS_PER_SECOND.set(rps)
    
    def record_domain_delay(self, delay: float):
        """Record per-domain crawl delay."""
        CRAWL_DOMAIN_DELAYS.observe(delay)
    
    def record_crawl_circuit_breaker_trip(self, domain: str):
        """Record circuit breaker trip."""
        CRAWL_CIRCUIT_BREAKER_TRIPS.labels(domain=domain).inc()
    
    def record_crawl_retry(self, reason: str):
        """Record crawl retry."""
        CRAWL_RETRIES.labels(reason=reason).inc()
    
    def record_dedup_skip(self):
        """Record URL skipped due to deduplication."""
        CRAWL_DEDUP_SKIPPED.inc()

    # ========================================================================
    # TRANSLATION PIPELINE METRICS
    # ========================================================================
    
    def update_translation_throughput(self, tokens_per_second: float):
        """Update translation throughput."""
        TRANSLATION_THROUGHPUT.set(tokens_per_second)
    
    def record_translation_batch(self, batch_size: int):
        """Record translation batch size."""
        TRANSLATION_BATCH_SIZE.observe(batch_size)
    
    def update_translation_queue(self, depth: int):
        """Update translation queue depth."""
        TRANSLATION_QUEUE_DEPTH.set(depth)
    
    def record_translation_cache_hit(self):
        """Record cache hit."""
        TRANSLATION_CACHE_HITS.inc()
    
    def record_translation_cache_miss(self):
        """Record cache miss."""
        TRANSLATION_CACHE_MISSES.inc()
    
    def record_translation_language_pair(self, source: str, target: str):
        """Record translation language pair."""
        TRANSLATION_LANGUAGE_PAIRS.labels(source_lang=source, target_lang=target).inc()
    
    def record_translation_quality(self, score: float):
        """Record translation quality score."""
        TRANSLATION_QUALITY_SCORE.observe(score)

    # ========================================================================
    # RAG METRICS
    # ========================================================================
    
    def record_rag_query(self, latency: float, docs_retrieved: int):
        """Record RAG query."""
        RAG_QUERY_LATENCY.observe(latency)
        RAG_DOCUMENTS_RETRIEVED.observe(docs_retrieved)
    
    def record_rag_relevance_score(self, score: float):
        """Record relevance score."""
        RAG_RELEVANCE_SCORES.observe(score)
    
    def update_rag_index_stats(self, total_docs: int, total_chunks: int):
        """Update RAG index statistics."""
        RAG_TOTAL_DOCUMENTS.set(total_docs)
        RAG_TOTAL_CHUNKS.set(total_chunks)

    # ========================================================================
    # PIPELINE METRICS
    # ========================================================================
    
    def record_pipeline_stage_duration(self, stage: str, duration: float):
        """Record pipeline stage duration."""
        PIPELINE_STAGE_DURATION.labels(stage=stage).observe(duration)
    
    def update_pipeline_documents_in_flight(self, count: int):
        """Update documents in pipeline."""
        PIPELINE_DOCUMENTS_IN_FLIGHT.set(count)
    
    def update_pipeline_stage_queue(self, stage: str, depth: int):
        """Update stage queue depth."""
        PIPELINE_STAGE_QUEUE_DEPTH.labels(stage=stage).set(depth)

    # ========================================================================
    # STORAGE METRICS
    # ========================================================================
    
    def record_storage_insert_latency(self, storage_type: str, latency: float):
        """Record storage insert latency."""
        STORAGE_INSERT_LATENCY.labels(storage_type=storage_type).observe(latency)
    
    def record_storage_query_latency(self, storage_type: str, query_type: str, latency: float):
        """Record storage query latency."""
        STORAGE_QUERY_LATENCY.labels(storage_type=storage_type, query_type=query_type).observe(latency)
    
    def update_storage_documents(self, storage_type: str, count: int):
        """Update total documents in storage."""
        STORAGE_DOCUMENTS_TOTAL.labels(storage_type=storage_type).set(count)
    
    def update_storage_size(self, storage_type: str, size_bytes: int):
        """Update storage size."""
        STORAGE_SIZE_BYTES.labels(storage_type=storage_type).set(size_bytes)
    
    def update_storage_connections(self, storage_type: str, count: int):
        """Update active storage connections."""
        STORAGE_CONNECTIONS_ACTIVE.labels(storage_type=storage_type).set(count)
    
    def record_storage_error(self, storage_type: str, operation: str, error_type: str):
        """Record storage error."""
        STORAGE_ERRORS.labels(
            storage_type=storage_type,
            operation=operation,
            error_type=error_type
        ).inc()

    @asynccontextmanager
    async def track_storage_operation(self, storage_type: str, operation: str = "insert"):
        """Context manager to track storage operation duration."""
        start_time = time.time()
        try:
            yield
        except Exception as e:
            self.record_storage_error(storage_type, operation, type(e).__name__)
            raise
        finally:
            duration = time.time() - start_time
            if operation == "insert":
                STORAGE_INSERT_LATENCY.labels(storage_type=storage_type).observe(duration)
            else:
                STORAGE_QUERY_LATENCY.labels(storage_type=storage_type, query_type=operation).observe(duration)

    # ========================================================================
    # TRANSLATION QUALITY METRICS
    # ========================================================================
    
    def record_translation_bleu_score(self, score: float):
        """Record BLEU score for translation quality."""
        TRANSLATION_BLEU_SCORE.observe(score)
    
    def update_translation_model_memory(self, memory_bytes: int):
        """Update translation model memory usage."""
        TRANSLATION_MODEL_MEMORY_BYTES.set(memory_bytes)
    
    def update_translation_gpu_utilization(self, percent: float):
        """Update GPU utilization percentage."""
        TRANSLATION_GPU_UTILIZATION.set(percent)

    # ========================================================================
    # SYSTEM HEALTH METRICS
    # ========================================================================
    
    def update_system_cpu(self, percent: float):
        """Update CPU usage percentage."""
        SYSTEM_CPU_USAGE.set(percent)
    
    def update_system_memory(self, percent: float):
        """Update memory usage percentage."""
        SYSTEM_MEMORY_USAGE.set(percent)
    
    def update_system_disk(self, mount_point: str, percent: float):
        """Update disk usage percentage."""
        SYSTEM_DISK_USAGE.labels(mount_point=mount_point).set(percent)
    
    def collect_system_metrics(self):
        """Collect current system metrics (CPU, memory, disk)."""
        try:
            import psutil
            
            # CPU usage
            self.update_system_cpu(psutil.cpu_percent(interval=None))
            
            # Memory usage
            memory = psutil.virtual_memory()
            self.update_system_memory(memory.percent)
            
            # Disk usage for root partition
            disk = psutil.disk_usage('/')
            self.update_system_disk('/', disk.percent)
            
        except ImportError:
            # psutil not available, skip system metrics
            pass
        except Exception as e:
            self.logger.warning(f"Failed to collect system metrics: {e}")

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
