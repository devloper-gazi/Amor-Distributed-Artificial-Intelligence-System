"""
Language detection using fastText.
Fast and accurate language detection supporting 150+ languages.
"""

import asyncio
import hashlib
from functools import lru_cache
from typing import Optional
from ftlangdetect import detect
from ..config.settings import settings
from ..config.logging_config import logger
from ..core.models import DetectedLanguage
from ..core.exceptions import LanguageDetectionError
from ..infrastructure.cache import cache_manager
from ..infrastructure.monitoring import monitor


# Language code to name mapping (common languages)
LANGUAGE_NAMES = {
    "en": "English",
    "es": "Spanish",
    "fr": "French",
    "de": "German",
    "it": "Italian",
    "pt": "Portuguese",
    "ru": "Russian",
    "ja": "Japanese",
    "ko": "Korean",
    "zh": "Chinese",
    "ar": "Arabic",
    "hi": "Hindi",
    "nl": "Dutch",
    "pl": "Polish",
    "tr": "Turkish",
    "vi": "Vietnamese",
    "id": "Indonesian",
    "th": "Thai",
    "uk": "Ukrainian",
    "ro": "Romanian",
    "el": "Greek",
    "cs": "Czech",
    "sv": "Swedish",
    "da": "Danish",
    "fi": "Finnish",
    "no": "Norwegian",
    "he": "Hebrew",
    "fa": "Persian",
    "bn": "Bengali",
    "ur": "Urdu",
    "ta": "Tamil",
    "te": "Telugu",
    "mr": "Marathi",
}


class LanguageDetector:
    """
    Language detector using fastText.

    Provides fast, accurate language detection with caching.
    """

    def __init__(self, use_cache: bool = True):
        """
        Initialize language detector.

        Args:
            use_cache: Whether to use caching
        """
        self.use_cache = use_cache
        self.low_memory = True  # Use low memory mode for efficiency
        self._cache_hits = 0
        self._cache_misses = 0

    @lru_cache(maxsize=10000)
    def _detect_cached(self, text_hash: str, text: str) -> dict:
        """
        Cached detection by text hash.

        Args:
            text_hash: Hash of text (for cache key)
            text: Text to detect

        Returns:
            Detection result dictionary
        """
        try:
            result = detect(text=text, low_memory=self.low_memory)
            return result
        except Exception as e:
            logger.error("language_detection_error", error=str(e))
            raise LanguageDetectionError(f"Language detection failed: {e}")

    async def detect_language(
        self,
        text: str,
        sample_size: int = 1000,
    ) -> DetectedLanguage:
        """
        Detect language of text.

        Args:
            text: Text to detect language of
            sample_size: Number of characters to use for detection

        Returns:
            DetectedLanguage object
        """
        if not text or len(text.strip()) < 10:
            logger.warning("language_detection_text_too_short", length=len(text))
            return DetectedLanguage(code="en", confidence=0.0)

        # Use sample for efficiency
        sample = text[:sample_size].strip()

        # Check cache first
        if self.use_cache:
            cache_key = self._get_cache_key(sample)
            cached = await cache_manager.get_json(cache_key)

            if cached:
                self._cache_hits += 1
                monitor.record_cache_operation("get", "hit")

                logger.debug(
                    "language_detection_cache_hit",
                    language=cached["code"],
                )

                detected = DetectedLanguage(**cached)
                monitor.record_language_detection(
                    detected.code,
                    detected.confidence
                )
                return detected

            self._cache_misses += 1
            monitor.record_cache_operation("get", "miss")

        # Run detection in thread pool to avoid blocking
        loop = asyncio.get_event_loop()
        text_hash = hashlib.md5(sample.encode()).hexdigest()

        try:
            result = await loop.run_in_executor(
                None,
                self._detect_cached,
                text_hash,
                sample,
            )

            # Create DetectedLanguage object
            detected = DetectedLanguage(
                code=result["lang"],
                name=LANGUAGE_NAMES.get(result["lang"], result["lang"]),
                confidence=result["score"],
            )

            # Cache result
            if self.use_cache:
                await cache_manager.set_json(
                    cache_key,
                    detected.model_dump(),
                    ttl=settings.redis_ttl,
                )

            # Record metrics
            monitor.record_language_detection(detected.code, detected.confidence)

            logger.debug(
                "language_detected",
                language=detected.code,
                confidence=detected.confidence,
                text_length=len(text),
            )

            return detected

        except Exception as e:
            logger.error("language_detection_failed", error=str(e))
            # Return English as fallback
            return DetectedLanguage(code="en", confidence=0.0)

    async def batch_detect(self, texts: list[str]) -> list[DetectedLanguage]:
        """
        Detect language for batch of texts.

        Args:
            texts: List of texts to detect

        Returns:
            List of DetectedLanguage objects
        """
        tasks = [self.detect_language(text) for text in texts]
        return await asyncio.gather(*tasks, return_exceptions=False)

    def _get_cache_key(self, text: str) -> str:
        """
        Generate cache key for text.

        Args:
            text: Text to generate key for

        Returns:
            Cache key string
        """
        text_hash = hashlib.sha256(text.encode()).hexdigest()
        return f"lang_detect:{text_hash}"

    def get_cache_stats(self) -> dict:
        """
        Get cache statistics.

        Returns:
            Dictionary with cache stats
        """
        total = self._cache_hits + self._cache_misses
        hit_rate = (
            (self._cache_hits / total * 100) if total > 0 else 0.0
        )

        return {
            "cache_hits": self._cache_hits,
            "cache_misses": self._cache_misses,
            "hit_rate": hit_rate,
        }

    def clear_cache(self):
        """Clear LRU cache."""
        self._detect_cached.cache_clear()
        self._cache_hits = 0
        self._cache_misses = 0
        logger.info("language_detector_cache_cleared")


# Global language detector instance
language_detector = LanguageDetector()
