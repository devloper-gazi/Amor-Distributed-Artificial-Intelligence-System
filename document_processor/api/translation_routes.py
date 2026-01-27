"""
Translation API Routes.

REST API endpoints for translation services including:
- Single text translation
- Batch translation
- Language detection
- Translation job management
"""

import asyncio
import logging
from datetime import datetime
from typing import Optional, List, Dict, Any
from uuid import uuid4

from fastapi import APIRouter, HTTPException, BackgroundTasks, Query
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/translate", tags=["Translation"])


# ============================================================================
# Request/Response Models
# ============================================================================

class TranslateRequest(BaseModel):
    """Request for single text translation."""
    text: str = Field(..., min_length=1, max_length=50000, description="Text to translate")
    source_language: Optional[str] = Field(None, description="Source language code (auto-detect if not provided)")
    target_language: str = Field(default="en", description="Target language code")
    use_cache: bool = Field(default=True, description="Use translation cache")


class BatchTranslateRequest(BaseModel):
    """Request for batch translation."""
    texts: List[str] = Field(..., min_items=1, max_items=100, description="Texts to translate")
    source_language: Optional[str] = Field(None, description="Source language (auto-detect if not provided)")
    target_language: str = Field(default="en", description="Target language")
    use_cache: bool = Field(default=True, description="Use translation cache")


class DetectLanguageRequest(BaseModel):
    """Request for language detection."""
    text: str = Field(..., min_length=1, max_length=10000, description="Text to analyze")


class TranslationResponse(BaseModel):
    """Response for single translation."""
    success: bool
    translation: Optional[str] = None
    source_language: Optional[str] = None
    target_language: str
    confidence: float = 0.0
    provider: Optional[str] = None
    from_cache: bool = False
    response_time: float = 0.0
    error: Optional[str] = None


class BatchTranslationResponse(BaseModel):
    """Response for batch translation."""
    success: bool
    results: List[TranslationResponse]
    total_items: int
    successful_items: int
    failed_items: int
    cached_items: int
    response_time: float


class LanguageDetectionResponse(BaseModel):
    """Response for language detection."""
    success: bool
    language: Optional[str] = None
    language_name: Optional[str] = None
    confidence: float = 0.0
    error: Optional[str] = None


class SupportedLanguage(BaseModel):
    """Supported language info."""
    code: str
    name: str
    nllb_code: str


class TranslationStatsResponse(BaseModel):
    """Translation service statistics."""
    total_translations: int = 0
    successful_translations: int = 0
    failed_translations: int = 0
    cached_translations: int = 0
    total_characters: int = 0
    cache_hit_rate: float = 0.0
    translations_per_provider: Dict[str, int] = Field(default_factory=dict)
    translations_per_language: Dict[str, int] = Field(default_factory=dict)


class TranslationJobRequest(BaseModel):
    """Request to create async translation job."""
    text: str = Field(..., min_length=1, max_length=100000)
    source_language: Optional[str] = None
    target_language: str = "en"
    priority: int = Field(default=0, ge=-100, le=100)
    metadata: Dict[str, Any] = Field(default_factory=dict)


class TranslationJobResponse(BaseModel):
    """Response for translation job."""
    job_id: str
    status: str
    result: Optional[str] = None
    confidence: Optional[float] = None
    provider: Optional[str] = None
    error: Optional[str] = None
    created_at: datetime
    completed_at: Optional[datetime] = None


# ============================================================================
# Translation Service Instance
# ============================================================================

_translation_service = None


async def _get_translation_service():
    """Get or create translation service."""
    global _translation_service
    
    if _translation_service is None:
        from ..services.translation_service import TranslationService, TranslationConfig
        
        config = TranslationConfig(
            redis_url="redis://redis:6379",
            cache_enabled=True,
            cache_ttl_days=7,
        )
        
        _translation_service = TranslationService(config)
        await _translation_service.initialize()
    
    return _translation_service


# ============================================================================
# API Endpoints
# ============================================================================

@router.post("/", response_model=TranslationResponse)
async def translate_text(request: TranslateRequest):
    """
    Translate text to target language.
    
    Supports 200+ languages using local NLLB-200 model.
    Source language is auto-detected if not provided.
    """
    import time
    start_time = time.time()
    
    try:
        service = await _get_translation_service()
        
        result = await service.translate(
            text=request.text,
            source_language=request.source_language,
            target_language=request.target_language,
            use_cache=request.use_cache,
        )
        
        response_time = time.time() - start_time
        
        return TranslationResponse(
            success=result.get("success", False),
            translation=result.get("translation"),
            source_language=result.get("source_language"),
            target_language=request.target_language,
            confidence=result.get("confidence", 0.0),
            provider=result.get("provider"),
            from_cache=result.get("from_cache", False),
            response_time=response_time,
            error=result.get("error"),
        )
        
    except Exception as e:
        logger.error(f"Translation failed: {e}")
        return TranslationResponse(
            success=False,
            target_language=request.target_language,
            response_time=time.time() - start_time,
            error=str(e),
        )


@router.post("/batch", response_model=BatchTranslationResponse)
async def batch_translate(request: BatchTranslateRequest):
    """
    Translate multiple texts in a batch.
    
    More efficient for translating many texts as it uses
    batch processing on the GPU.
    """
    import time
    start_time = time.time()
    
    try:
        service = await _get_translation_service()
        
        results = await service.batch_translate(
            texts=request.texts,
            source_language=request.source_language,
            target_language=request.target_language,
        )
        
        response_time = time.time() - start_time
        
        # Convert results
        responses = []
        successful = 0
        failed = 0
        cached = 0
        
        for result in results:
            response = TranslationResponse(
                success=result.get("success", False),
                translation=result.get("translation"),
                source_language=result.get("source_language"),
                target_language=request.target_language,
                confidence=result.get("confidence", 0.0),
                provider=result.get("provider"),
                from_cache=result.get("from_cache", False),
                response_time=0.0,
                error=result.get("error"),
            )
            responses.append(response)
            
            if result.get("success"):
                successful += 1
                if result.get("from_cache"):
                    cached += 1
            else:
                failed += 1
        
        return BatchTranslationResponse(
            success=failed == 0,
            results=responses,
            total_items=len(request.texts),
            successful_items=successful,
            failed_items=failed,
            cached_items=cached,
            response_time=response_time,
        )
        
    except Exception as e:
        logger.error(f"Batch translation failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/detect", response_model=LanguageDetectionResponse)
async def detect_language(request: DetectLanguageRequest):
    """
    Detect the language of text.
    
    Uses FastText language detection model supporting 176 languages.
    """
    try:
        from fasttext_langdetect import detect
        
        # Use first 1000 chars for efficiency
        result = detect(request.text[:1000])
        
        # Get language name
        from local_ai.translation.nllb_translator import LANGUAGE_NAMES
        language_name = LANGUAGE_NAMES.get(result["lang"], result["lang"])
        
        return LanguageDetectionResponse(
            success=True,
            language=result["lang"],
            language_name=language_name,
            confidence=result["score"],
        )
        
    except ImportError:
        return LanguageDetectionResponse(
            success=False,
            error="Language detection not available (fasttext-langdetect not installed)",
        )
    except Exception as e:
        logger.error(f"Language detection failed: {e}")
        return LanguageDetectionResponse(
            success=False,
            error=str(e),
        )


@router.get("/languages", response_model=List[SupportedLanguage])
async def list_supported_languages():
    """
    List all supported languages.
    
    Returns ISO codes, names, and NLLB codes for each supported language.
    """
    try:
        from local_ai.translation.nllb_translator import LANGUAGE_CODES, LANGUAGE_NAMES
        
        languages = []
        for code, nllb_code in LANGUAGE_CODES.items():
            # Skip variants and 3-letter codes to avoid duplicates
            if len(code) > 3 or "_" in code:
                continue
            
            name = LANGUAGE_NAMES.get(code, code)
            languages.append(SupportedLanguage(
                code=code,
                name=name,
                nllb_code=nllb_code,
            ))
        
        # Sort by name
        languages.sort(key=lambda x: x.name)
        
        return languages
        
    except Exception as e:
        logger.error(f"Failed to list languages: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/stats", response_model=TranslationStatsResponse)
async def get_translation_stats():
    """
    Get translation service statistics.
    """
    try:
        service = await _get_translation_service()
        stats = service.get_stats()
        
        return TranslationStatsResponse(
            total_translations=stats.total_translations,
            successful_translations=stats.successful_translations,
            failed_translations=stats.failed_translations,
            cached_translations=stats.cached_translations,
            total_characters=stats.total_characters,
            cache_hit_rate=stats.cache_hit_rate,
            translations_per_provider=stats.translations_by_provider,
            translations_per_language=stats.translations_by_language_pair,
        )
        
    except Exception as e:
        logger.error(f"Failed to get stats: {e}")
        return TranslationStatsResponse()


@router.post("/job", response_model=TranslationJobResponse)
async def create_translation_job(request: TranslationJobRequest):
    """
    Create an async translation job.
    
    For long texts or when you don't need immediate results,
    use this endpoint to queue a translation job.
    """
    try:
        service = await _get_translation_service()
        
        job_id = await service.enqueue_translation(
            text=request.text,
            source_language=request.source_language,
            target_language=request.target_language,
            priority=request.priority,
            metadata=request.metadata,
        )
        
        return TranslationJobResponse(
            job_id=job_id,
            status="pending",
            created_at=datetime.utcnow(),
        )
        
    except Exception as e:
        logger.error(f"Failed to create translation job: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/job/{job_id}", response_model=TranslationJobResponse)
async def get_translation_job(job_id: str):
    """
    Get status of a translation job.
    """
    try:
        service = await _get_translation_service()
        job = await service.get_job_status(job_id)
        
        if not job:
            raise HTTPException(status_code=404, detail=f"Job {job_id} not found")
        
        return TranslationJobResponse(
            job_id=job["id"],
            status=job["status"],
            result=job.get("result"),
            confidence=job.get("confidence"),
            provider=job.get("provider"),
            error=job.get("error"),
            created_at=datetime.fromisoformat(job["created_at"]),
            completed_at=datetime.fromisoformat(job["completed_at"]) if job.get("completed_at") else None,
        )
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to get job status: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/health")
async def translation_health():
    """
    Check translation service health.
    """
    try:
        service = await _get_translation_service()
        
        # Check if translator is initialized
        languages = service.get_supported_languages()
        
        return {
            "status": "healthy",
            "provider": "nllb-200",
            "languages_supported": len(languages),
            "cache_enabled": service.config.cache_enabled,
        }
        
    except Exception as e:
        return {
            "status": "unhealthy",
            "error": str(e),
        }
