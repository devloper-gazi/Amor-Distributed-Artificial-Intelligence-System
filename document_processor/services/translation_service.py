"""
High-Throughput Translation Service.

Production-grade translation service with:
- Batch processing for efficiency
- Kafka queue integration for async jobs
- Redis caching for translation memory
- Quality assurance with language verification
- Multi-provider fallback (NLLB → Cloud APIs)
"""

import asyncio
import hashlib
import json
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum
from typing import Optional, List, Dict, Any, Callable, Awaitable
from uuid import uuid4

import redis.asyncio as redis

logger = logging.getLogger(__name__)

# Optional imports
try:
    from aiokafka import AIOKafkaProducer, AIOKafkaConsumer
    HAS_KAFKA = True
except ImportError:
    HAS_KAFKA = False
    logger.warning("aiokafka not installed. Kafka integration disabled.")

try:
    from fasttext_langdetect import detect as detect_language
    HAS_LANGDETECT = True
except ImportError:
    HAS_LANGDETECT = False
    logger.warning("fasttext_langdetect not installed. Language detection disabled.")


class TranslationStatus(Enum):
    """Translation job status."""
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    FAILED = "failed"
    SKIPPED = "skipped"


class TranslationProvider(Enum):
    """Translation providers."""
    NLLB_LOCAL = "nllb_local"
    CLAUDE = "claude"
    GOOGLE = "google"
    AZURE = "azure"


@dataclass
class TranslationConfig:
    """Configuration for translation service."""
    # Provider settings
    primary_provider: TranslationProvider = TranslationProvider.NLLB_LOCAL
    fallback_providers: List[TranslationProvider] = field(
        default_factory=lambda: [TranslationProvider.CLAUDE]
    )
    
    # Batch settings
    batch_size: int = 16
    max_batch_wait_seconds: float = 5.0
    max_concurrent_batches: int = 4
    
    # Caching
    cache_enabled: bool = True
    cache_ttl_days: int = 7
    
    # Quality settings
    min_confidence_threshold: float = 0.5
    verify_output_language: bool = True
    
    # Kafka settings
    kafka_enabled: bool = False
    kafka_bootstrap_servers: str = "localhost:9092"
    kafka_topic: str = "translations"
    kafka_consumer_group: str = "translation_workers"
    
    # Redis settings
    redis_url: str = "redis://localhost:6379"
    
    # Rate limiting
    max_chars_per_minute: int = 100000
    max_requests_per_minute: int = 100
    
    # Timeouts
    translation_timeout: float = 60.0
    
    # NLLB model settings
    nllb_model_path: str = "/models/nllb-200-distilled-600M-ct2"
    nllb_device: str = "cuda"
    nllb_compute_type: str = "int8_float16"


@dataclass
class TranslationJob:
    """A translation job."""
    id: str
    text: str
    source_language: Optional[str]
    target_language: str
    status: TranslationStatus
    priority: int = 0
    created_at: datetime = field(default_factory=datetime.utcnow)
    completed_at: Optional[datetime] = None
    result: Optional[str] = None
    confidence: Optional[float] = None
    provider: Optional[str] = None
    error: Optional[str] = None
    metadata: Dict[str, Any] = field(default_factory=dict)
    
    def to_dict(self) -> Dict[str, Any]:
        """Serialize to dictionary."""
        return {
            "id": self.id,
            "text": self.text,
            "source_language": self.source_language,
            "target_language": self.target_language,
            "status": self.status.value,
            "priority": self.priority,
            "created_at": self.created_at.isoformat(),
            "completed_at": self.completed_at.isoformat() if self.completed_at else None,
            "result": self.result,
            "confidence": self.confidence,
            "provider": self.provider,
            "error": self.error,
            "metadata": self.metadata,
        }
    
    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "TranslationJob":
        """Deserialize from dictionary."""
        return cls(
            id=data["id"],
            text=data["text"],
            source_language=data.get("source_language"),
            target_language=data["target_language"],
            status=TranslationStatus(data["status"]),
            priority=data.get("priority", 0),
            created_at=datetime.fromisoformat(data["created_at"]),
            completed_at=datetime.fromisoformat(data["completed_at"]) if data.get("completed_at") else None,
            result=data.get("result"),
            confidence=data.get("confidence"),
            provider=data.get("provider"),
            error=data.get("error"),
            metadata=data.get("metadata", {}),
        )


@dataclass
class TranslationStats:
    """Translation service statistics."""
    total_translations: int = 0
    successful_translations: int = 0
    failed_translations: int = 0
    cached_translations: int = 0
    total_characters: int = 0
    total_tokens: int = 0
    average_confidence: float = 0.0
    translations_per_second: float = 0.0
    cache_hit_rate: float = 0.0
    errors_by_type: Dict[str, int] = field(default_factory=dict)
    translations_by_provider: Dict[str, int] = field(default_factory=dict)
    translations_by_language_pair: Dict[str, int] = field(default_factory=dict)


class TranslationCache:
    """
    Redis-backed translation cache with content-addressable storage.
    
    Key format: translation:{hash}
    Hash: SHA256(text + source_lang + target_lang)
    """
    
    def __init__(
        self,
        redis_client: redis.Redis,
        key_prefix: str = "translation",
        ttl_days: int = 7,
    ):
        self.redis = redis_client
        self.key_prefix = key_prefix
        self.ttl_seconds = ttl_days * 24 * 60 * 60
        
        self._hits = 0
        self._misses = 0
    
    def _compute_key(self, text: str, source_lang: str, target_lang: str) -> str:
        """Compute cache key from content."""
        content = f"{text}|{source_lang}|{target_lang}"
        hash_value = hashlib.sha256(content.encode()).hexdigest()
        return f"{self.key_prefix}:{hash_value}"
    
    async def get(
        self,
        text: str,
        source_lang: str,
        target_lang: str,
    ) -> Optional[Dict[str, Any]]:
        """Get cached translation."""
        key = self._compute_key(text, source_lang, target_lang)
        
        cached = await self.redis.get(key)
        if cached:
            self._hits += 1
            return json.loads(cached)
        
        self._misses += 1
        return None
    
    async def set(
        self,
        text: str,
        source_lang: str,
        target_lang: str,
        translation: str,
        confidence: float,
        provider: str,
    ):
        """Cache a translation."""
        key = self._compute_key(text, source_lang, target_lang)
        
        value = {
            "translation": translation,
            "source_language": source_lang,
            "target_language": target_lang,
            "confidence": confidence,
            "provider": provider,
            "cached_at": datetime.utcnow().isoformat(),
        }
        
        await self.redis.setex(
            key,
            self.ttl_seconds,
            json.dumps(value),
        )
    
    @property
    def hit_rate(self) -> float:
        """Get cache hit rate."""
        total = self._hits + self._misses
        if total == 0:
            return 0.0
        return self._hits / total


class TranslationService:
    """
    High-throughput translation service with batching and caching.
    
    Features:
    - Batch processing for GPU efficiency
    - Redis translation memory
    - Kafka queue for async processing
    - Multi-provider fallback
    - Quality verification
    - Rate limiting
    """
    
    def __init__(self, config: Optional[TranslationConfig] = None):
        """
        Initialize translation service.
        
        Args:
            config: Service configuration
        """
        self.config = config or TranslationConfig()
        
        # Components
        self._redis: Optional[redis.Redis] = None
        self._cache: Optional[TranslationCache] = None
        self._translator = None
        self._kafka_producer = None
        self._kafka_consumer = None
        
        # Batch processing
        self._batch_queue: asyncio.Queue = asyncio.Queue()
        self._batch_processor_task: Optional[asyncio.Task] = None
        
        # Rate limiting
        self._request_times: List[float] = []
        self._char_counts: List[tuple] = []  # (timestamp, char_count)
        
        # Statistics
        self.stats = TranslationStats()
        
        # State
        self._initialized = False
        self._running = False
    
    async def initialize(self):
        """Initialize the translation service."""
        if self._initialized:
            return
        
        logger.info("Initializing translation service...")
        
        # Initialize Redis
        self._redis = redis.from_url(
            self.config.redis_url,
            encoding="utf-8",
            decode_responses=True,
        )
        await self._redis.ping()
        
        # Initialize cache
        if self.config.cache_enabled:
            self._cache = TranslationCache(
                self._redis,
                ttl_days=self.config.cache_ttl_days,
            )
        
        # Initialize NLLB translator
        if self.config.primary_provider == TranslationProvider.NLLB_LOCAL:
            try:
                from local_ai.translation.nllb_translator import NLLBTranslator
                
                self._translator = NLLBTranslator(
                    model_path=self.config.nllb_model_path,
                    device=self.config.nllb_device,
                    compute_type=self.config.nllb_compute_type,
                    max_batch_size=self.config.batch_size,
                )
                logger.info("NLLB translator initialized")
            except Exception as e:
                logger.warning(f"Failed to initialize NLLB translator: {e}")
        
        # Initialize Kafka if enabled
        if self.config.kafka_enabled and HAS_KAFKA:
            try:
                self._kafka_producer = AIOKafkaProducer(
                    bootstrap_servers=self.config.kafka_bootstrap_servers,
                    value_serializer=lambda v: json.dumps(v).encode(),
                )
                await self._kafka_producer.start()
                logger.info("Kafka producer initialized")
            except Exception as e:
                logger.warning(f"Failed to initialize Kafka: {e}")
        
        self._initialized = True
        logger.info("Translation service initialized")
    
    async def start(self):
        """Start background processing."""
        if self._running:
            return
        
        await self.initialize()
        
        self._running = True
        
        # Start batch processor
        self._batch_processor_task = asyncio.create_task(
            self._batch_processor_loop()
        )
        
        logger.info("Translation service started")
    
    async def stop(self):
        """Stop the service."""
        self._running = False
        
        if self._batch_processor_task:
            self._batch_processor_task.cancel()
            try:
                await self._batch_processor_task
            except asyncio.CancelledError:
                pass
        
        if self._kafka_producer:
            await self._kafka_producer.stop()
        
        if self._redis:
            await self._redis.close()
        
        logger.info("Translation service stopped")
    
    async def _detect_language(self, text: str) -> tuple:
        """
        Detect language of text.
        
        Returns:
            (language_code, confidence)
        """
        if not HAS_LANGDETECT:
            return None, 0.0
        
        try:
            # Use first 1000 chars for efficiency
            result = detect_language(text[:1000])
            return result["lang"], result["score"]
        except Exception as e:
            logger.warning(f"Language detection failed: {e}")
            return None, 0.0
    
    async def _check_rate_limits(self, char_count: int) -> bool:
        """Check if we're within rate limits."""
        current_time = time.time()
        
        # Clean old entries
        self._request_times = [
            t for t in self._request_times 
            if current_time - t < 60
        ]
        self._char_counts = [
            (t, c) for t, c in self._char_counts 
            if current_time - t < 60
        ]
        
        # Check request rate
        if len(self._request_times) >= self.config.max_requests_per_minute:
            return False
        
        # Check character rate
        total_chars = sum(c for _, c in self._char_counts)
        if total_chars + char_count > self.config.max_chars_per_minute:
            return False
        
        return True
    
    async def _update_rate_limits(self, char_count: int):
        """Update rate limit tracking."""
        current_time = time.time()
        self._request_times.append(current_time)
        self._char_counts.append((current_time, char_count))
    
    async def translate(
        self,
        text: str,
        source_language: Optional[str] = None,
        target_language: str = "en",
        use_cache: bool = True,
        detect_language_if_missing: bool = True,
    ) -> Dict[str, Any]:
        """
        Translate text to target language.
        
        Args:
            text: Text to translate
            source_language: Source language code (auto-detect if None)
            target_language: Target language code
            use_cache: Whether to use translation cache
            detect_language_if_missing: Auto-detect if source not provided
            
        Returns:
            Translation result dictionary
        """
        if not self._initialized:
            await self.initialize()
        
        start_time = time.time()
        
        # Detect language if not provided
        if not source_language and detect_language_if_missing:
            source_language, detection_confidence = await self._detect_language(text)
            if not source_language:
                return {
                    "success": False,
                    "error": "Could not detect source language",
                }
        
        # Skip if already in target language
        if source_language == target_language:
            self.stats.total_translations += 1
            return {
                "success": True,
                "translation": text,
                "source_language": source_language,
                "target_language": target_language,
                "skipped": True,
                "reason": "Already in target language",
            }
        
        # Check cache
        if use_cache and self._cache:
            cached = await self._cache.get(text, source_language, target_language)
            if cached:
                self.stats.cached_translations += 1
                self.stats.total_translations += 1
                cached["from_cache"] = True
                cached["success"] = True
                return cached
        
        # Check rate limits
        if not await self._check_rate_limits(len(text)):
            return {
                "success": False,
                "error": "Rate limit exceeded",
                "retry_after": 60,
            }
        
        # Translate
        try:
            result = await self._do_translate(text, source_language, target_language)
            
            # Update stats
            await self._update_rate_limits(len(text))
            self.stats.total_translations += 1
            self.stats.total_characters += len(text)
            
            if result.get("success"):
                self.stats.successful_translations += 1
                
                # Update provider stats
                provider = result.get("provider", "unknown")
                self.stats.translations_by_provider[provider] = \
                    self.stats.translations_by_provider.get(provider, 0) + 1
                
                # Update language pair stats
                lang_pair = f"{source_language}->{target_language}"
                self.stats.translations_by_language_pair[lang_pair] = \
                    self.stats.translations_by_language_pair.get(lang_pair, 0) + 1
                
                # Cache result
                if self._cache and result.get("translation"):
                    await self._cache.set(
                        text,
                        source_language,
                        target_language,
                        result["translation"],
                        result.get("confidence", 0.0),
                        result.get("provider", "unknown"),
                    )
            else:
                self.stats.failed_translations += 1
            
            result["response_time"] = time.time() - start_time
            result["from_cache"] = False
            
            return result
            
        except Exception as e:
            logger.error(f"Translation failed: {e}")
            self.stats.failed_translations += 1
            self.stats.errors_by_type[type(e).__name__] = \
                self.stats.errors_by_type.get(type(e).__name__, 0) + 1
            
            return {
                "success": False,
                "error": str(e),
                "response_time": time.time() - start_time,
            }
    
    async def _do_translate(
        self,
        text: str,
        source_language: str,
        target_language: str,
    ) -> Dict[str, Any]:
        """Perform actual translation using configured provider."""
        
        # Try primary provider
        if self._translator:
            try:
                result = await asyncio.wait_for(
                    self._translator.translate(
                        text,
                        source_language,
                        target_language,
                    ),
                    timeout=self.config.translation_timeout,
                )
                
                result["success"] = True
                
                # Verify output language if enabled
                if self.config.verify_output_language:
                    detected, _ = await self._detect_language(result["translation"])
                    if detected and detected != target_language:
                        logger.warning(
                            f"Output language mismatch: expected {target_language}, "
                            f"got {detected}"
                        )
                        result["language_verified"] = False
                    else:
                        result["language_verified"] = True
                
                return result
                
            except asyncio.TimeoutError:
                logger.warning("Translation timed out, trying fallback")
            except Exception as e:
                logger.warning(f"Primary translator failed: {e}")
        
        # Try fallback providers
        for provider in self.config.fallback_providers:
            try:
                result = await self._translate_with_provider(
                    text, source_language, target_language, provider
                )
                if result.get("success"):
                    return result
            except Exception as e:
                logger.warning(f"Fallback provider {provider} failed: {e}")
        
        return {
            "success": False,
            "error": "All translation providers failed",
        }
    
    async def _translate_with_provider(
        self,
        text: str,
        source_language: str,
        target_language: str,
        provider: TranslationProvider,
    ) -> Dict[str, Any]:
        """Translate using a specific provider."""
        
        if provider == TranslationProvider.CLAUDE:
            # Use Claude API for translation
            try:
                import anthropic
                
                client = anthropic.AsyncAnthropic()
                
                message = await client.messages.create(
                    model="claude-3-haiku-20240307",
                    max_tokens=1024,
                    messages=[
                        {
                            "role": "user",
                            "content": f"Translate the following text from {source_language} to {target_language}. Only output the translation, nothing else.\n\n{text}"
                        }
                    ]
                )
                
                return {
                    "success": True,
                    "translation": message.content[0].text,
                    "source_language": source_language,
                    "target_language": target_language,
                    "provider": "claude",
                    "confidence": 0.9,
                }
                
            except Exception as e:
                logger.error(f"Claude translation failed: {e}")
                return {"success": False, "error": str(e)}
        
        return {"success": False, "error": f"Provider {provider} not implemented"}
    
    async def batch_translate(
        self,
        texts: List[str],
        source_language: Optional[str] = None,
        target_language: str = "en",
    ) -> List[Dict[str, Any]]:
        """
        Translate multiple texts in a batch (more efficient).
        
        Args:
            texts: List of texts to translate
            source_language: Source language (auto-detect if None)
            target_language: Target language
            
        Returns:
            List of translation results
        """
        if not self._initialized:
            await self.initialize()
        
        if not texts:
            return []
        
        # Detect language from first text if not provided
        if not source_language:
            source_language, _ = await self._detect_language(texts[0])
            if not source_language:
                return [{"success": False, "error": "Could not detect language"}] * len(texts)
        
        # Check cache for each text
        results = []
        uncached_texts = []
        uncached_indices = []
        
        if self._cache:
            for i, text in enumerate(texts):
                cached = await self._cache.get(text, source_language, target_language)
                if cached:
                    cached["from_cache"] = True
                    cached["success"] = True
                    results.append((i, cached))
                    self.stats.cached_translations += 1
                else:
                    uncached_texts.append(text)
                    uncached_indices.append(i)
        else:
            uncached_texts = texts
            uncached_indices = list(range(len(texts)))
        
        # Batch translate uncached texts
        if uncached_texts and self._translator:
            try:
                batch_results = await self._translator.batch_translate(
                    uncached_texts,
                    source_language,
                    target_language,
                )
                
                for idx, result in zip(uncached_indices, batch_results):
                    result["success"] = True
                    result["from_cache"] = False
                    results.append((idx, result))
                    
                    # Cache result
                    if self._cache:
                        await self._cache.set(
                            uncached_texts[batch_results.index(result)],
                            source_language,
                            target_language,
                            result["translation"],
                            result.get("confidence", 0.0),
                            result.get("provider", "unknown"),
                        )
                
                self.stats.successful_translations += len(batch_results)
                
            except Exception as e:
                logger.error(f"Batch translation failed: {e}")
                for idx in uncached_indices:
                    results.append((idx, {"success": False, "error": str(e)}))
                self.stats.failed_translations += len(uncached_indices)
        
        # Sort by original index and return
        results.sort(key=lambda x: x[0])
        return [r for _, r in results]
    
    async def enqueue_translation(
        self,
        text: str,
        source_language: Optional[str] = None,
        target_language: str = "en",
        priority: int = 0,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> str:
        """
        Enqueue a translation job for async processing.
        
        Args:
            text: Text to translate
            source_language: Source language
            target_language: Target language
            priority: Job priority (higher = sooner)
            metadata: Optional metadata
            
        Returns:
            Job ID
        """
        job = TranslationJob(
            id=str(uuid4()),
            text=text,
            source_language=source_language,
            target_language=target_language,
            status=TranslationStatus.PENDING,
            priority=priority,
            metadata=metadata or {},
        )
        
        # Store job in Redis
        job_key = f"translation_job:{job.id}"
        await self._redis.setex(
            job_key,
            86400,  # 24 hour TTL
            json.dumps(job.to_dict()),
        )
        
        # Send to Kafka if enabled
        if self._kafka_producer:
            await self._kafka_producer.send_and_wait(
                self.config.kafka_topic,
                job.to_dict(),
            )
        else:
            # Use local queue
            await self._batch_queue.put(job)
        
        logger.debug(f"Translation job enqueued: {job.id}")
        return job.id
    
    async def get_job_status(self, job_id: str) -> Optional[Dict[str, Any]]:
        """Get status of a translation job."""
        job_key = f"translation_job:{job_id}"
        data = await self._redis.get(job_key)
        
        if data:
            return json.loads(data)
        return None
    
    async def _batch_processor_loop(self):
        """Background loop for processing batched translations."""
        batch = []
        last_batch_time = time.time()
        
        while self._running:
            try:
                # Try to get job from queue
                try:
                    job = await asyncio.wait_for(
                        self._batch_queue.get(),
                        timeout=self.config.max_batch_wait_seconds,
                    )
                    batch.append(job)
                except asyncio.TimeoutError:
                    pass
                
                # Process batch if full or timeout reached
                should_process = (
                    len(batch) >= self.config.batch_size or
                    (batch and time.time() - last_batch_time >= self.config.max_batch_wait_seconds)
                )
                
                if should_process and batch:
                    await self._process_batch(batch)
                    batch = []
                    last_batch_time = time.time()
                
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Batch processor error: {e}")
                await asyncio.sleep(1)
        
        # Process remaining batch
        if batch:
            await self._process_batch(batch)
    
    async def _process_batch(self, jobs: List[TranslationJob]):
        """Process a batch of translation jobs."""
        logger.info(f"Processing batch of {len(jobs)} translations")
        
        # Group by language pair
        by_lang_pair: Dict[tuple, List[TranslationJob]] = {}
        for job in jobs:
            key = (job.source_language, job.target_language)
            if key not in by_lang_pair:
                by_lang_pair[key] = []
            by_lang_pair[key].append(job)
        
        # Process each language pair
        for (source_lang, target_lang), group_jobs in by_lang_pair.items():
            texts = [j.text for j in group_jobs]
            
            try:
                results = await self.batch_translate(
                    texts,
                    source_lang,
                    target_lang,
                )
                
                # Update jobs with results
                for job, result in zip(group_jobs, results):
                    job.status = TranslationStatus.COMPLETED if result.get("success") else TranslationStatus.FAILED
                    job.result = result.get("translation")
                    job.confidence = result.get("confidence")
                    job.provider = result.get("provider")
                    job.error = result.get("error")
                    job.completed_at = datetime.utcnow()
                    
                    # Update in Redis
                    job_key = f"translation_job:{job.id}"
                    await self._redis.setex(
                        job_key,
                        86400,
                        json.dumps(job.to_dict()),
                    )
                    
            except Exception as e:
                logger.error(f"Batch processing failed: {e}")
                for job in group_jobs:
                    job.status = TranslationStatus.FAILED
                    job.error = str(e)
                    job.completed_at = datetime.utcnow()
                    
                    job_key = f"translation_job:{job.id}"
                    await self._redis.setex(
                        job_key,
                        86400,
                        json.dumps(job.to_dict()),
                    )
    
    def get_stats(self) -> TranslationStats:
        """Get service statistics."""
        if self._cache:
            self.stats.cache_hit_rate = self._cache.hit_rate
        return self.stats
    
    def get_supported_languages(self) -> List[str]:
        """Get list of supported languages."""
        if self._translator:
            return self._translator.get_supported_languages()
        return []
    
    async def __aenter__(self):
        await self.start()
        return self
    
    async def __aexit__(self, exc_type, exc_val, exc_tb):
        await self.stop()
