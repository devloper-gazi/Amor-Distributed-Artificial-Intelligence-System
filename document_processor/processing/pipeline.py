"""
Main processing pipeline orchestration.
Coordinates source processing, language detection, translation, and storage.
"""

import asyncio
import time
from typing import List
from datetime import datetime
from ..config.settings import settings
from ..config.logging_config import logger
from ..core.models import (
    SourceDocument,
    TranslatedDocument,
    ProcessingStatus,
    ProcessingMetrics,
    DetectedLanguage,
)
from ..infrastructure.monitoring import monitor
from ..infrastructure.cache import cache_manager
from ..infrastructure.queue import queue_manager
from ..infrastructure.storage import storage_manager
from ..processing.language_detector import language_detector
from ..processing.translator import translation_router
from ..processing.deduplicator import deduplicator
from ..processing.quality_checker import quality_checker
from ..sources.web_scraper import WebScraper
from ..sources.pdf_processor import PDFProcessor
from ..sources.database import DatabaseConnector
from ..sources.api_client import APIClient
from ..sources.file_reader import FileReader
from ..reliability.error_handler import ErrorHandler


class ProcessingPipeline:
    """
    Main processing pipeline.

    Orchestrates the entire document processing workflow from
    ingestion to translation and storage.
    """

    def __init__(self):
        """Initialize processing pipeline."""
        self.settings = settings
        self.metrics = ProcessingMetrics()
        self.error_handler = ErrorHandler(max_retries=settings.max_retries)

        # Initialize source processors
        self.processors = {
            "web": WebScraper(settings),
            "pdf": PDFProcessor(settings),
            "sql": DatabaseConnector(settings),
            "nosql": DatabaseConnector(settings),
            "api": APIClient(settings),
            "file": FileReader(settings),
        }

        # Concurrency control
        self.semaphore = asyncio.Semaphore(settings.max_concurrent_sources)
        self._running = False

    async def start(self):
        """Start the processing pipeline."""
        logger.info(
            "pipeline_starting",
            max_concurrent=settings.max_concurrent_sources,
            batch_size=settings.batch_size,
        )

        self._running = True

        # Connect infrastructure components
        await cache_manager.connect()
        await storage_manager.connect_postgres()
        await storage_manager.connect_mongo()

        logger.info("pipeline_started")

    async def stop(self):
        """Stop the processing pipeline."""
        logger.info("pipeline_stopping")
        self._running = False

        # Disconnect infrastructure
        await cache_manager.disconnect()
        await storage_manager.disconnect()

        logger.info("pipeline_stopped")

    async def process_source(self, source: SourceDocument) -> TranslatedDocument:
        """
        Process single source document.

        Args:
            source: Source document to process

        Returns:
            Translated document
        """
        start_time = time.time()

        async with self.semaphore:
            monitor.record_document_in_progress(source.source_type, 1)

            try:
                # Get appropriate processor
                processor = self.processors.get(source.source_type)
                if not processor or not await processor.can_process(source):
                    raise ValueError(f"No processor for {source.source_type}")

                # Extract content (streaming)
                all_chunks = []

                async with monitor.track_extraction_duration(source.source_type):
                    async for chunk in processor.extract_content(source):
                        # Check for duplicates
                        if not deduplicator.is_duplicate(chunk):
                            all_chunks.append(chunk)

                # Combine chunks
                full_text = "\n\n".join(all_chunks)

                if not full_text.strip():
                    logger.warning("empty_content_extracted", source_id=source.id)
                    raise ValueError("No content extracted")

                # Detect language
                detected_lang = await language_detector.detect_language(full_text)

                logger.info(
                    "language_detected",
                    source_id=source.id,
                    language=detected_lang.code,
                    confidence=detected_lang.confidence,
                )

                # Translate
                translation_result = await translation_router.translate(
                    text=full_text,
                    source_lang=detected_lang.code,
                    target_lang="en",
                    priority=source.priority,
                )

                # Quality check
                quality_metrics = quality_checker.check_quality(
                    original=full_text,
                    translated=translation_result.text,
                    source_lang=detected_lang.code,
                    target_lang="en",
                )

                # Create result document
                processing_time_ms = (time.time() - start_time) * 1000

                result = TranslatedDocument(
                    source_id=source.id,
                    original_language=detected_lang,
                    original_text=full_text[:5000],  # Store sample
                    translated_text=translation_result.text,
                    translation_provider=translation_result.provider,
                    translation_quality_score=quality_metrics["overall_score"],
                    processing_time_ms=processing_time_ms,
                    cached=translation_result.cached,
                    status=ProcessingStatus.COMPLETED,
                    completed_at=datetime.utcnow(),
                )

                # Store result
                await storage_manager.save_document(result)

                # Update metrics
                self.metrics.processed += 1
                self.metrics.total_processing_time_ms += processing_time_ms
                self.metrics.avg_processing_time_ms = (
                    self.metrics.total_processing_time_ms / self.metrics.processed
                )
                self.metrics.languages_detected[detected_lang.code] = (
                    self.metrics.languages_detected.get(detected_lang.code, 0) + 1
                )
                self.metrics.providers_used[str(translation_result.provider)] = (
                    self.metrics.providers_used.get(str(translation_result.provider), 0) + 1
                )

                if translation_result.cached:
                    self.metrics.cache_hits += 1
                else:
                    self.metrics.cache_misses += 1

                # Record monitoring metrics
                monitor.record_document_processed(source.source_type, "success")
                monitor.record_source_extraction(source.source_type, "success")

                logger.info(
                    "document_processed_successfully",
                    source_id=source.id,
                    source_type=source.source_type,
                    language=detected_lang.code,
                    provider=translation_result.provider,
                    cached=translation_result.cached,
                    processing_time_ms=processing_time_ms,
                    quality_score=quality_metrics["overall_score"],
                )

                return result

            except Exception as e:
                # Handle error
                self.metrics.failed += 1
                monitor.record_document_processed(source.source_type, "failed")
                monitor.record_error(type(e).__name__, "pipeline")

                logger.error(
                    "document_processing_failed",
                    source_id=source.id,
                    source_type=source.source_type,
                    error=str(e),
                    exc_info=True,
                )

                # Create failed document
                result = TranslatedDocument(
                    source_id=source.id,
                    original_language=DetectedLanguage(code="unknown", confidence=0.0),
                    original_text="",
                    translated_text="",
                    translation_provider="none",
                    processing_time_ms=(time.time() - start_time) * 1000,
                    status=ProcessingStatus.FAILED,
                    error=str(e),
                )

                await storage_manager.save_document(result)

                raise

            finally:
                monitor.record_document_in_progress(source.source_type, -1)

    async def process_batch(
        self,
        sources: List[SourceDocument],
    ) -> List[TranslatedDocument]:
        """
        Process batch of sources concurrently.

        Args:
            sources: List of source documents

        Returns:
            List of translated documents
        """
        self.metrics.total_sources = len(sources)
        self.metrics.start_time = datetime.utcnow()

        logger.info(
            "batch_processing_started",
            total_sources=len(sources),
        )

        # Process all sources concurrently
        tasks = [self.process_source(source) for source in sources]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        self.metrics.end_time = datetime.utcnow()

        # Separate successful and failed results
        successful = [r for r in results if not isinstance(r, Exception)]
        failed = [r for r in results if isinstance(r, Exception)]

        logger.info(
            "batch_processing_completed",
            total=len(sources),
            successful=len(successful),
            failed=len(failed),
            duration_seconds=self.metrics.duration_seconds,
            throughput=self.metrics.throughput_docs_per_second,
            cache_hit_rate=self.metrics.cache_hit_rate,
            dedup_stats=deduplicator.get_stats(),
        )

        return successful

    def get_metrics(self) -> ProcessingMetrics:
        """Get current processing metrics."""
        return self.metrics

    def reset_metrics(self):
        """Reset processing metrics."""
        self.metrics = ProcessingMetrics()
        logger.info("metrics_reset")


# Global pipeline instance
pipeline = ProcessingPipeline()
