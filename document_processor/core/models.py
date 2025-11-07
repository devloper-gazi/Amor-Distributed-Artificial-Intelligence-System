"""
Core data models for the document processing system.
All models use Pydantic for validation and serialization.
"""

from pydantic import BaseModel, Field, HttpUrl, field_validator
from typing import Optional, Dict, Any, List
from datetime import datetime
from enum import Enum
import uuid


class SourceType(str, Enum):
    """Supported source types for document ingestion."""
    WEB = "web"
    PDF = "pdf"
    SQL = "sql"
    NOSQL = "nosql"
    API = "api"
    FILE = "file"


class ProcessingStatus(str, Enum):
    """Document processing status."""
    PENDING = "pending"
    PROCESSING = "processing"
    LANGUAGE_DETECTED = "language_detected"
    TRANSLATING = "translating"
    TRANSLATED = "translated"
    COMPLETED = "completed"
    FAILED = "failed"
    SKIPPED = "skipped"


class TranslationProvider(str, Enum):
    """Translation service providers."""
    NONE = "none"  # No translation needed (already English)
    CACHE = "cache"  # Retrieved from cache
    CLAUDE = "claude"  # Claude 3.5 Sonnet (highest quality)
    GOOGLE = "google"  # Google Cloud Translation
    AZURE = "azure"  # Azure Translator
    NLLB = "nllb"  # NLLB-200 (self-hosted)


class SourceDocument(BaseModel):
    """Source document to be processed."""
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    source_type: SourceType
    source_url: Optional[str] = None
    source_path: Optional[str] = None
    metadata: Dict[str, Any] = Field(default_factory=dict)
    priority: str = "balanced"  # quality, balanced, volume
    created_at: datetime = Field(default_factory=datetime.utcnow)

    @field_validator('source_url', 'source_path')
    @classmethod
    def validate_source(cls, v, info):
        """Ensure at least one source is provided."""
        # This will be called for each field, so we can't validate across fields here
        # We'll add a model_validator instead
        return v

    class Config:
        use_enum_values = True
        json_encoders = {
            datetime: lambda v: v.isoformat(),
        }


class DetectedLanguage(BaseModel):
    """Detected language information."""
    code: str  # ISO 639-1 language code
    name: Optional[str] = None
    confidence: float = Field(ge=0.0, le=1.0)

    class Config:
        json_schema_extra = {
            "example": {
                "code": "es",
                "name": "Spanish",
                "confidence": 0.95
            }
        }


class TranslationResult(BaseModel):
    """Result of a translation operation."""
    text: str
    provider: TranslationProvider
    cached: bool = False
    quality_score: Optional[float] = Field(None, ge=0.0, le=1.0)
    tokens_used: Optional[int] = None
    cost_estimate: Optional[float] = None

    class Config:
        use_enum_values = True


class TranslatedDocument(BaseModel):
    """Processed and translated document."""
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    source_id: str
    original_language: DetectedLanguage
    original_text: str
    original_text_length: int = 0
    translated_text: str
    translated_text_length: int = 0
    translation_provider: TranslationProvider
    translation_quality_score: Optional[float] = Field(None, ge=0.0, le=1.0)
    processing_time_ms: float
    cached: bool = False
    status: ProcessingStatus
    error: Optional[str] = None
    retry_count: int = 0
    metadata: Dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=datetime.utcnow)
    completed_at: Optional[datetime] = None

    def __init__(self, **data):
        super().__init__(**data)
        if not self.original_text_length:
            self.original_text_length = len(self.original_text)
        if not self.translated_text_length:
            self.translated_text_length = len(self.translated_text)

    class Config:
        use_enum_values = True
        json_encoders = {
            datetime: lambda v: v.isoformat() if v else None,
        }


class ProcessingMetrics(BaseModel):
    """Processing metrics and statistics."""
    total_sources: int = 0
    processed: int = 0
    failed: int = 0
    skipped: int = 0
    cache_hits: int = 0
    cache_misses: int = 0
    avg_processing_time_ms: float = 0.0
    total_processing_time_ms: float = 0.0
    languages_detected: Dict[str, int] = Field(default_factory=dict)
    providers_used: Dict[str, int] = Field(default_factory=dict)
    total_characters_processed: int = 0
    total_characters_translated: int = 0
    start_time: datetime = Field(default_factory=datetime.utcnow)
    end_time: Optional[datetime] = None

    @property
    def success_rate(self) -> float:
        """Calculate success rate percentage."""
        if self.total_sources == 0:
            return 0.0
        return (self.processed / self.total_sources) * 100

    @property
    def cache_hit_rate(self) -> float:
        """Calculate cache hit rate percentage."""
        total_requests = self.cache_hits + self.cache_misses
        if total_requests == 0:
            return 0.0
        return (self.cache_hits / total_requests) * 100

    @property
    def duration_seconds(self) -> float:
        """Calculate total duration in seconds."""
        if not self.end_time:
            return (datetime.utcnow() - self.start_time).total_seconds()
        return (self.end_time - self.start_time).total_seconds()

    @property
    def throughput_docs_per_second(self) -> float:
        """Calculate throughput in documents per second."""
        duration = self.duration_seconds
        if duration == 0:
            return 0.0
        return self.processed / duration

    class Config:
        json_encoders = {
            datetime: lambda v: v.isoformat() if v else None,
        }


class HealthStatus(BaseModel):
    """System health status."""
    status: str  # healthy, degraded, unhealthy
    timestamp: datetime = Field(default_factory=datetime.utcnow)
    components: Dict[str, bool] = Field(default_factory=dict)
    message: Optional[str] = None

    class Config:
        json_encoders = {
            datetime: lambda v: v.isoformat(),
        }


class BatchProcessingRequest(BaseModel):
    """Request to process a batch of documents."""
    sources: List[SourceDocument]
    priority: str = "balanced"
    async_processing: bool = True
    callback_url: Optional[str] = None

    @field_validator('sources')
    @classmethod
    def validate_sources(cls, v):
        """Validate sources list is not empty."""
        if not v:
            raise ValueError("Sources list cannot be empty")
        if len(v) > 10000:
            raise ValueError("Maximum batch size is 10000 documents")
        return v


class BatchProcessingResponse(BaseModel):
    """Response for batch processing request."""
    batch_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    submitted: int
    estimated_completion_time_seconds: Optional[float] = None
    status_url: Optional[str] = None
    timestamp: datetime = Field(default_factory=datetime.utcnow)

    class Config:
        json_encoders = {
            datetime: lambda v: v.isoformat(),
        }
