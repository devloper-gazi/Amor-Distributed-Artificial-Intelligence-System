"""
Crawl Pipeline Orchestration.

Defines processing pipeline stages for web scraping:
URL Frontier → Fetch → Extract → Detect Language → Translate → Embed → Store
"""

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Optional, List, Dict, Any, Callable, Awaitable

logger = logging.getLogger(__name__)


class PipelineStage(Enum):
    """Pipeline processing stages."""
    URL_FRONTIER = "url_frontier"
    FETCH = "fetch"
    EXTRACT = "extract"
    DETECT_LANGUAGE = "detect_language"
    TRANSLATE = "translate"
    EMBED = "embed"
    STORE = "store"


@dataclass
class PipelineConfig:
    """Configuration for crawl pipeline."""
    # Stage enabling
    enable_translation: bool = True
    enable_embedding: bool = True
    
    # Concurrency per stage
    fetch_concurrency: int = 50
    extract_concurrency: int = 20
    translate_concurrency: int = 10
    embed_concurrency: int = 20
    
    # Batch sizes
    translation_batch_size: int = 16
    embedding_batch_size: int = 32
    storage_batch_size: int = 100
    
    # Timeouts
    fetch_timeout: float = 30.0
    extract_timeout: float = 10.0
    translate_timeout: float = 60.0
    embed_timeout: float = 30.0
    
    # Target language
    target_language: str = "en"


@dataclass
class PipelineDocument:
    """Document flowing through the pipeline."""
    id: str
    url: str
    
    # Content (populated by stages)
    raw_html: Optional[str] = None
    cleaned_text: Optional[str] = None
    translated_text: Optional[str] = None
    title: Optional[str] = None
    
    # Metadata
    domain: Optional[str] = None
    status_code: Optional[int] = None
    response_time: float = 0.0
    
    # Language
    detected_language: Optional[str] = None
    target_language: str = "en"
    language_confidence: float = 0.0
    translation_confidence: float = 0.0
    
    # Embedding
    embedding: Optional[List[float]] = None
    
    # Pipeline state
    current_stage: PipelineStage = PipelineStage.URL_FRONTIER
    completed_stages: List[str] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)
    
    # Timestamps
    created_at: datetime = field(default_factory=datetime.utcnow)
    completed_at: Optional[datetime] = None
    
    # Additional metadata
    metadata: Dict[str, Any] = field(default_factory=dict)
    
    def to_dict(self) -> Dict[str, Any]:
        """Serialize to dictionary."""
        return {
            "id": self.id,
            "url": self.url,
            "domain": self.domain,
            "title": self.title,
            "detected_language": self.detected_language,
            "target_language": self.target_language,
            "status_code": self.status_code,
            "response_time": self.response_time,
            "current_stage": self.current_stage.value,
            "completed_stages": self.completed_stages,
            "errors": self.errors,
            "created_at": self.created_at.isoformat(),
            "completed_at": self.completed_at.isoformat() if self.completed_at else None,
            "metadata": self.metadata,
        }


@dataclass
class PipelineStats:
    """Pipeline execution statistics."""
    documents_processed: int = 0
    documents_failed: int = 0
    documents_in_pipeline: int = 0
    
    # Per-stage stats
    stage_counts: Dict[str, int] = field(default_factory=dict)
    stage_times: Dict[str, float] = field(default_factory=dict)
    stage_errors: Dict[str, int] = field(default_factory=dict)
    
    # Throughput
    documents_per_second: float = 0.0
    bytes_processed: int = 0


class CrawlPipeline:
    """
    Multi-stage crawl processing pipeline.
    
    Stages:
    1. URL Frontier: Get URLs to process
    2. Fetch: Download web pages
    3. Extract: Extract content from HTML
    4. Detect Language: Identify content language
    5. Translate: Translate to target language
    6. Embed: Generate vector embeddings
    7. Store: Save to database and vector store
    
    Each stage processes documents concurrently and
    passes results to the next stage.
    """
    
    def __init__(self, config: Optional[PipelineConfig] = None):
        """
        Initialize pipeline.
        
        Args:
            config: Pipeline configuration
        """
        self.config = config or PipelineConfig()
        
        # Stage handlers
        self._handlers: Dict[PipelineStage, Callable[[PipelineDocument], Awaitable[PipelineDocument]]] = {}
        
        # Queues between stages
        self._queues: Dict[PipelineStage, asyncio.Queue] = {
            stage: asyncio.Queue() for stage in PipelineStage
        }
        
        # Stage workers
        self._workers: Dict[PipelineStage, List[asyncio.Task]] = {
            stage: [] for stage in PipelineStage
        }
        
        # Statistics
        self.stats = PipelineStats()
        
        # State
        self._running = False
        self._initialized = False
    
    async def initialize(self):
        """Initialize pipeline components."""
        if self._initialized:
            return
        
        # Set up default handlers
        self._setup_default_handlers()
        
        self._initialized = True
        logger.info("Pipeline initialized")
    
    def _setup_default_handlers(self):
        """Set up default stage handlers."""
        
        # Fetch stage
        async def fetch_handler(doc: PipelineDocument) -> PipelineDocument:
            from ..crawling.resilient_scraper import ResilientScraper
            
            async with ResilientScraper() as scraper:
                result = await scraper.scrape(doc.url)
                
                doc.raw_html = result.content
                doc.status_code = result.status_code
                doc.response_time = result.response_time
                doc.title = result.title
                doc.cleaned_text = result.text
                doc.domain = scraper._get_domain(doc.url)
                
                if not result.success:
                    doc.errors.append(f"Fetch failed: {result.error_message}")
            
            return doc
        
        self._handlers[PipelineStage.FETCH] = fetch_handler
        
        # Extract stage (if fetching didn't extract)
        async def extract_handler(doc: PipelineDocument) -> PipelineDocument:
            if doc.cleaned_text:
                return doc
            
            if not doc.raw_html:
                doc.errors.append("No HTML content to extract")
                return doc
            
            try:
                import trafilatura
                
                extracted = trafilatura.bare_extraction(
                    doc.raw_html,
                    url=doc.url,
                    include_links=False,
                )
                
                if extracted:
                    doc.cleaned_text = extracted.get("text", "")
                    doc.title = doc.title or extracted.get("title")
                    doc.detected_language = extracted.get("language")
                
            except Exception as e:
                doc.errors.append(f"Extract failed: {e}")
            
            return doc
        
        self._handlers[PipelineStage.EXTRACT] = extract_handler
        
        # Language detection stage
        async def detect_language_handler(doc: PipelineDocument) -> PipelineDocument:
            if not doc.cleaned_text:
                return doc
            
            if doc.detected_language:
                return doc
            
            try:
                from fasttext_langdetect import detect
                
                result = detect(doc.cleaned_text[:1000])
                doc.detected_language = result["lang"]
                doc.language_confidence = result["score"]
                
            except Exception as e:
                doc.errors.append(f"Language detection failed: {e}")
            
            return doc
        
        self._handlers[PipelineStage.DETECT_LANGUAGE] = detect_language_handler
        
        # Translation stage
        async def translate_handler(doc: PipelineDocument) -> PipelineDocument:
            if not self.config.enable_translation:
                return doc
            
            if not doc.cleaned_text:
                return doc
            
            # Skip if already in target language
            if doc.detected_language == self.config.target_language:
                doc.translated_text = doc.cleaned_text
                doc.translation_confidence = 1.0
                return doc
            
            try:
                from ..services.translation_service import TranslationService
                
                service = TranslationService()
                await service.initialize()
                
                result = await service.translate(
                    text=doc.cleaned_text,
                    source_language=doc.detected_language,
                    target_language=self.config.target_language,
                )
                
                if result.get("success"):
                    doc.translated_text = result.get("translation")
                    doc.translation_confidence = result.get("confidence", 0.0)
                else:
                    doc.errors.append(f"Translation failed: {result.get('error')}")
                    
            except Exception as e:
                doc.errors.append(f"Translation failed: {e}")
            
            return doc
        
        self._handlers[PipelineStage.TRANSLATE] = translate_handler
        
        # Embedding stage
        async def embed_handler(doc: PipelineDocument) -> PipelineDocument:
            if not self.config.enable_embedding:
                return doc
            
            text_to_embed = doc.translated_text or doc.cleaned_text
            if not text_to_embed:
                return doc
            
            try:
                from sentence_transformers import SentenceTransformer
                
                model = SentenceTransformer(
                    "nomic-ai/nomic-embed-text-v1.5",
                    device="cpu",
                )
                
                embedding = model.encode(
                    text_to_embed[:8000],  # Limit length
                    normalize_embeddings=True,
                )
                
                doc.embedding = embedding.tolist()
                
            except Exception as e:
                doc.errors.append(f"Embedding failed: {e}")
            
            return doc
        
        self._handlers[PipelineStage.EMBED] = embed_handler
        
        # Store stage
        async def store_handler(doc: PipelineDocument) -> PipelineDocument:
            try:
                # Store in database (placeholder)
                doc.completed_at = datetime.utcnow()
                doc.metadata["stored"] = True
                
                logger.debug(f"Document stored: {doc.id}")
                
            except Exception as e:
                doc.errors.append(f"Storage failed: {e}")
            
            return doc
        
        self._handlers[PipelineStage.STORE] = store_handler
    
    def register_handler(
        self,
        stage: PipelineStage,
        handler: Callable[[PipelineDocument], Awaitable[PipelineDocument]],
    ):
        """
        Register a custom handler for a stage.
        
        Args:
            stage: Pipeline stage
            handler: Async handler function
        """
        self._handlers[stage] = handler
    
    async def start(self):
        """Start pipeline processing."""
        if self._running:
            return
        
        await self.initialize()
        self._running = True
        
        # Start stage workers
        stage_concurrency = {
            PipelineStage.FETCH: self.config.fetch_concurrency,
            PipelineStage.EXTRACT: self.config.extract_concurrency,
            PipelineStage.DETECT_LANGUAGE: 10,
            PipelineStage.TRANSLATE: self.config.translate_concurrency,
            PipelineStage.EMBED: self.config.embed_concurrency,
            PipelineStage.STORE: 10,
        }
        
        for stage in PipelineStage:
            if stage == PipelineStage.URL_FRONTIER:
                continue  # URL frontier is input, not a worker stage
            
            concurrency = stage_concurrency.get(stage, 5)
            
            for i in range(concurrency):
                worker = asyncio.create_task(
                    self._stage_worker(stage)
                )
                self._workers[stage].append(worker)
        
        logger.info("Pipeline started")
    
    async def stop(self):
        """Stop pipeline processing."""
        self._running = False
        
        # Cancel all workers
        for stage, workers in self._workers.items():
            for worker in workers:
                worker.cancel()
        
        logger.info("Pipeline stopped")
    
    async def submit(self, doc: PipelineDocument):
        """
        Submit a document to the pipeline.
        
        Args:
            doc: Document to process
        """
        doc.current_stage = PipelineStage.FETCH
        await self._queues[PipelineStage.FETCH].put(doc)
        self.stats.documents_in_pipeline += 1
    
    async def submit_url(self, url: str, **kwargs) -> str:
        """
        Submit a URL for processing.
        
        Args:
            url: URL to process
            **kwargs: Additional document attributes
            
        Returns:
            Document ID
        """
        import hashlib
        
        doc_id = hashlib.sha256(url.encode()).hexdigest()[:16]
        
        doc = PipelineDocument(
            id=doc_id,
            url=url,
            **kwargs,
        )
        
        await self.submit(doc)
        return doc_id
    
    async def _stage_worker(self, stage: PipelineStage):
        """Worker coroutine for a pipeline stage."""
        queue = self._queues[stage]
        handler = self._handlers.get(stage)
        
        if not handler:
            logger.error(f"No handler for stage: {stage}")
            return
        
        # Determine next stage
        stages = list(PipelineStage)
        stage_idx = stages.index(stage)
        next_stage = stages[stage_idx + 1] if stage_idx < len(stages) - 1 else None
        
        while self._running:
            try:
                # Get document from queue
                doc = await asyncio.wait_for(
                    queue.get(),
                    timeout=1.0,
                )
                
                # Process document
                import time
                start_time = time.time()
                
                try:
                    doc = await handler(doc)
                    doc.completed_stages.append(stage.value)
                    
                    # Update stats
                    self.stats.stage_counts[stage.value] = \
                        self.stats.stage_counts.get(stage.value, 0) + 1
                    
                except Exception as e:
                    doc.errors.append(f"{stage.value}: {e}")
                    self.stats.stage_errors[stage.value] = \
                        self.stats.stage_errors.get(stage.value, 0) + 1
                
                elapsed = time.time() - start_time
                self.stats.stage_times[stage.value] = \
                    (self.stats.stage_times.get(stage.value, 0) + elapsed) / 2
                
                # Pass to next stage or complete
                if next_stage and not doc.errors:
                    doc.current_stage = next_stage
                    await self._queues[next_stage].put(doc)
                else:
                    # Pipeline complete
                    doc.completed_at = datetime.utcnow()
                    self.stats.documents_in_pipeline -= 1
                    
                    if doc.errors:
                        self.stats.documents_failed += 1
                        logger.warning(f"Document {doc.id} failed: {doc.errors}")
                    else:
                        self.stats.documents_processed += 1
                        logger.debug(f"Document {doc.id} completed")
                
            except asyncio.TimeoutError:
                continue
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Worker error in {stage}: {e}")
    
    def get_stats(self) -> PipelineStats:
        """Get pipeline statistics."""
        return self.stats
    
    async def get_queue_sizes(self) -> Dict[str, int]:
        """Get current queue sizes."""
        return {
            stage.value: self._queues[stage].qsize()
            for stage in PipelineStage
        }
    
    async def __aenter__(self):
        await self.start()
        return self
    
    async def __aexit__(self, exc_type, exc_val, exc_tb):
        await self.stop()
