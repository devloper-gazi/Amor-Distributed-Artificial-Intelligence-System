"""
Multi-tier translation router with intelligent fallback.
Routes translations to optimal provider based on quality, cost, and availability.
"""

import hashlib
import aiohttp
from typing import Optional
from anthropic import AsyncAnthropic
from ..config.settings import settings
from ..config.logging_config import logger
from ..core.models import TranslationResult, TranslationProvider
from ..core.exceptions import TranslationError, TranslationAPIError, TranslationRateLimitError
from ..infrastructure.cache import cache_manager
from ..infrastructure.monitoring import monitor
from ..reliability.rate_limiter import RateLimiter
from ..reliability.circuit_breaker import CircuitBreaker
from ..reliability.retry import retry_decorator
from .language_detector import language_detector


class TranslationRouter:
    """
    Multi-tier translation router.

    Routes translation requests to appropriate provider based on:
    - Priority (quality vs cost vs volume)
    - Provider availability
    - Rate limits
    - Circuit breaker status
    """

    def __init__(self):
        """Initialize translation router."""
        # Initialize API clients
        self.anthropic = None
        if settings.anthropic_api_key:
            self.anthropic = AsyncAnthropic(api_key=settings.anthropic_api_key)

        # Rate limiters per provider
        self.rate_limiters = {
            "claude": RateLimiter(settings.anthropic_rpm),
            "google": RateLimiter(settings.google_translate_rpm),
            "azure": RateLimiter(settings.azure_translate_rpm),
        }

        # Circuit breakers per provider
        self.circuit_breakers = {
            "claude": CircuitBreaker(failure_threshold=5, recovery_timeout=60),
            "google": CircuitBreaker(failure_threshold=5, recovery_timeout=60),
            "azure": CircuitBreaker(failure_threshold=5, recovery_timeout=60),
        }

        # HTTP session for API calls
        self.session: Optional[aiohttp.ClientSession] = None

    async def __aenter__(self):
        """Async context manager entry."""
        self.session = aiohttp.ClientSession()
        return self

    async def __aexit__(self, *args):
        """Async context manager exit."""
        if self.session:
            await self.session.close()

    async def translate(
        self,
        text: str,
        source_lang: Optional[str] = None,
        target_lang: str = "en",
        priority: str = "balanced",
    ) -> TranslationResult:
        """
        Translate text using optimal provider.

        Args:
            text: Text to translate
            source_lang: Source language code (auto-detected if None)
            target_lang: Target language code
            priority: Priority level (quality, balanced, volume)

        Returns:
            TranslationResult
        """
        # Detect source language if not provided
        if not source_lang:
            detected = await language_detector.detect_language(text)
            source_lang = detected.code

        # Skip translation if already in target language
        if source_lang == target_lang:
            logger.debug("translation_not_needed", source_lang=source_lang)
            return TranslationResult(
                text=text,
                provider=TranslationProvider.NONE,
                cached=False,
                quality_score=1.0,
            )

        # Check cache first
        if settings.translation_cache_enabled:
            cache_key = self._get_cache_key(text, source_lang, target_lang)
            cached = await cache_manager.get(cache_key)

            if cached:
                monitor.record_translation("cache", source_lang, "success")
                logger.debug("translation_cache_hit", source_lang=source_lang)

                return TranslationResult(
                    text=cached,
                    provider=TranslationProvider.CACHE,
                    cached=True,
                )

        # Route to appropriate provider based on priority
        providers = self._get_provider_order(priority)

        last_error = None
        for provider_name in providers:
            try:
                # Use circuit breaker and rate limiter
                async with monitor.track_translation_duration(provider_name):
                    result = await self.circuit_breakers[provider_name].call(
                        self._translate_with_provider,
                        provider_name,
                        text,
                        source_lang,
                        target_lang,
                    )

                # Cache successful translation
                if settings.translation_cache_enabled:
                    await cache_manager.set(
                        cache_key,
                        result.text,
                        ttl=settings.redis_ttl,
                    )

                # Record metrics
                monitor.record_translation(
                    provider_name,
                    source_lang,
                    "success",
                    characters=len(text),
                    cost=self._estimate_cost(provider_name, len(text)),
                )

                logger.info(
                    "translation_success",
                    provider=provider_name,
                    source_lang=source_lang,
                    target_lang=target_lang,
                    text_length=len(text),
                )

                return result

            except Exception as e:
                last_error = e
                logger.warning(
                    "translation_provider_failed",
                    provider=provider_name,
                    error=str(e),
                )
                monitor.record_translation(provider_name, source_lang, "failed")
                continue

        # All providers failed
        error_msg = f"All translation providers failed. Last error: {last_error}"
        logger.error("translation_failed_all_providers", error=error_msg)
        raise TranslationError(error_msg)

    async def _translate_with_provider(
        self,
        provider: str,
        text: str,
        source_lang: str,
        target_lang: str,
    ) -> TranslationResult:
        """
        Translate with specific provider.

        Args:
            provider: Provider name
            text: Text to translate
            source_lang: Source language
            target_lang: Target language

        Returns:
            TranslationResult
        """
        # Apply rate limiting
        async with self.rate_limiters[provider]:
            if provider == "claude":
                return await self._translate_claude(text, source_lang, target_lang)
            elif provider == "google":
                return await self._translate_google(text, source_lang, target_lang)
            elif provider == "azure":
                return await self._translate_azure(text, source_lang, target_lang)
            else:
                raise ValueError(f"Unknown provider: {provider}")

    @retry_decorator(max_attempts=2, base_delay=1.0)
    async def _translate_claude(
        self,
        text: str,
        source_lang: str,
        target_lang: str,
    ) -> TranslationResult:
        """Translate using Claude 3.5 Sonnet (highest quality)."""
        if not self.anthropic:
            raise TranslationAPIError(
                "Claude API key not configured",
                provider="claude",
            )

        try:
            prompt = f"""Translate the following {source_lang} text to {target_lang}.
Preserve all context, nuance, tone, and formatting. Maintain technical accuracy and cultural references.
Provide ONLY the {target_lang} translation, no explanations or notes.

Text to translate:
{text}"""

            message = await self.anthropic.messages.create(
                model="claude-3-5-sonnet-20241022",
                max_tokens=8192,
                temperature=0.3,
                messages=[{"role": "user", "content": prompt}],
            )

            translated = message.content[0].text.strip()

            return TranslationResult(
                text=translated,
                provider=TranslationProvider.CLAUDE,
                cached=False,
                quality_score=0.95,
                tokens_used=message.usage.input_tokens + message.usage.output_tokens,
            )

        except Exception as e:
            logger.error("claude_translation_error", error=str(e))
            raise TranslationAPIError(str(e), provider="claude")

    @retry_decorator(max_attempts=3, base_delay=1.0)
    async def _translate_google(
        self,
        text: str,
        source_lang: str,
        target_lang: str,
    ) -> TranslationResult:
        """Translate using Google Cloud Translation (balanced quality/cost)."""
        if not settings.google_translate_api_key:
            raise TranslationAPIError(
                "Google Translate API key not configured",
                provider="google",
            )

        url = "https://translation.googleapis.com/language/translate/v2"
        params = {
            "key": settings.google_translate_api_key,
            "q": text,
            "source": source_lang,
            "target": target_lang,
            "format": "text",
        }

        if not self.session:
            self.session = aiohttp.ClientSession()

        try:
            async with self.session.post(url, json=params) as resp:
                if resp.status == 429:
                    raise TranslationRateLimitError(
                        "Google Translate rate limit exceeded",
                        provider="google",
                        retry_after=60,
                    )

                if resp.status != 200:
                    error = await resp.text()
                    raise TranslationAPIError(
                        f"Google Translate error: {error}",
                        provider="google",
                        status_code=resp.status,
                    )

                data = await resp.json()
                translated = data["data"]["translations"][0]["translatedText"]

                return TranslationResult(
                    text=translated,
                    provider=TranslationProvider.GOOGLE,
                    cached=False,
                    quality_score=0.90,
                )

        except aiohttp.ClientError as e:
            logger.error("google_translation_error", error=str(e))
            raise TranslationAPIError(str(e), provider="google")

    @retry_decorator(max_attempts=3, base_delay=1.0)
    async def _translate_azure(
        self,
        text: str,
        source_lang: str,
        target_lang: str,
    ) -> TranslationResult:
        """Translate using Azure Translator (high volume)."""
        if not settings.azure_translator_key:
            raise TranslationAPIError(
                "Azure Translator key not configured",
                provider="azure",
            )

        url = "https://api.cognitive.microsofttranslator.com/translate"
        params = {
            "api-version": "3.0",
            "from": source_lang,
            "to": target_lang,
        }
        headers = {
            "Ocp-Apim-Subscription-Key": settings.azure_translator_key,
            "Ocp-Apim-Subscription-Region": settings.azure_translator_region,
            "Content-Type": "application/json",
        }
        body = [{"text": text}]

        if not self.session:
            self.session = aiohttp.ClientSession()

        try:
            async with self.session.post(
                url,
                params=params,
                headers=headers,
                json=body,
            ) as resp:
                if resp.status == 429:
                    raise TranslationRateLimitError(
                        "Azure Translator rate limit exceeded",
                        provider="azure",
                        retry_after=60,
                    )

                if resp.status != 200:
                    error = await resp.text()
                    raise TranslationAPIError(
                        f"Azure Translator error: {error}",
                        provider="azure",
                        status_code=resp.status,
                    )

                data = await resp.json()
                translated = data[0]["translations"][0]["text"]

                return TranslationResult(
                    text=translated,
                    provider=TranslationProvider.AZURE,
                    cached=False,
                    quality_score=0.88,
                )

        except aiohttp.ClientError as e:
            logger.error("azure_translation_error", error=str(e))
            raise TranslationAPIError(str(e), provider="azure")

    def _get_provider_order(self, priority: str) -> list[str]:
        """
        Get provider order based on priority.

        Args:
            priority: Priority level

        Returns:
            List of provider names in order
        """
        if priority == "quality":
            return ["claude", "google", "azure"]
        elif priority == "balanced":
            return ["google", "azure", "claude"]
        else:  # volume
            return ["azure", "google", "claude"]

    def _get_cache_key(
        self,
        text: str,
        source_lang: str,
        target_lang: str,
    ) -> str:
        """Generate cache key for translation."""
        content = f"{text}:{source_lang}:{target_lang}"
        text_hash = hashlib.sha256(content.encode()).hexdigest()
        return f"translation:{text_hash}"

    def _estimate_cost(self, provider: str, char_count: int) -> float:
        """
        Estimate translation cost.

        Args:
            provider: Provider name
            char_count: Number of characters

        Returns:
            Estimated cost in USD
        """
        # Rough cost estimates per 1M characters
        costs = {
            "claude": 3.00,  # Claude pricing
            "google": 20.00,  # Google Translate pricing
            "azure": 10.00,  # Azure Translator pricing
        }

        cost_per_char = costs.get(provider, 0) / 1_000_000
        return char_count * cost_per_char

    async def batch_translate(
        self,
        texts: list[str],
        source_langs: Optional[list[str]] = None,
        target_lang: str = "en",
        priority: str = "balanced",
    ) -> list[TranslationResult]:
        """
        Batch translate multiple texts.

        Args:
            texts: List of texts to translate
            source_langs: List of source language codes
            target_lang: Target language code
            priority: Priority level

        Returns:
            List of TranslationResult objects
        """
        if source_langs is None:
            source_langs = [None] * len(texts)

        tasks = [
            self.translate(text, source_lang, target_lang, priority)
            for text, source_lang in zip(texts, source_langs)
        ]

        return await asyncio.gather(*tasks, return_exceptions=False)


# Global translation router instance
translation_router = TranslationRouter()
