"""
Translation quality checker.
Validates and scores translation quality using various heuristics.
"""

import re
from typing import Optional
from ..config.logging_config import logger
from ..core.models import TranslationResult


class QualityChecker:
    """
    Translation quality checker.

    Validates translations and provides quality scores based on:
    - Length ratio
    - Character diversity
    - Format preservation
    - Special character handling
    """

    def __init__(self, min_quality_score: float = 0.7):
        """
        Initialize quality checker.

        Args:
            min_quality_score: Minimum acceptable quality score
        """
        self.min_quality_score = min_quality_score

    def check_quality(
        self,
        original: str,
        translated: str,
        source_lang: str,
        target_lang: str,
    ) -> dict:
        """
        Check translation quality.

        Args:
            original: Original text
            translated: Translated text
            source_lang: Source language code
            target_lang: Target language code

        Returns:
            Dictionary with quality metrics
        """
        scores = []

        # Length ratio check
        length_score = self._check_length_ratio(original, translated)
        scores.append(length_score)

        # Character diversity check
        diversity_score = self._check_character_diversity(translated)
        scores.append(diversity_score)

        # Format preservation check
        format_score = self._check_format_preservation(original, translated)
        scores.append(format_score)

        # Special characters check
        special_score = self._check_special_characters(original, translated)
        scores.append(special_score)

        # Overall quality score (weighted average)
        overall_score = sum(scores) / len(scores)

        quality_metrics = {
            "overall_score": overall_score,
            "length_score": length_score,
            "diversity_score": diversity_score,
            "format_score": format_score,
            "special_score": special_score,
            "passed": overall_score >= self.min_quality_score,
        }

        if overall_score < self.min_quality_score:
            logger.warning(
                "low_quality_translation",
                score=overall_score,
                threshold=self.min_quality_score,
                **quality_metrics,
            )

        return quality_metrics

    def _check_length_ratio(self, original: str, translated: str) -> float:
        """
        Check if translation length is reasonable.

        Args:
            original: Original text
            translated: Translated text

        Returns:
            Quality score (0.0-1.0)
        """
        if not original or not translated:
            return 0.0

        ratio = len(translated) / len(original)

        # Acceptable range: 0.5x to 2.5x original length
        if 0.5 <= ratio <= 2.5:
            # Ideal range: 0.8x to 1.5x
            if 0.8 <= ratio <= 1.5:
                return 1.0
            else:
                # Penalize but still acceptable
                return 0.8
        else:
            # Outside acceptable range
            if ratio < 0.3 or ratio > 3.0:
                return 0.3
            return 0.5

    def _check_character_diversity(self, text: str) -> float:
        """
        Check character diversity in text.

        Args:
            text: Text to check

        Returns:
            Quality score (0.0-1.0)
        """
        if not text:
            return 0.0

        # Calculate unique character ratio
        unique_chars = len(set(text.lower()))
        total_chars = len(text)

        if total_chars == 0:
            return 0.0

        diversity_ratio = unique_chars / total_chars

        # Good diversity: 0.1 to 0.5
        if 0.1 <= diversity_ratio <= 0.5:
            return 1.0
        elif diversity_ratio < 0.05:
            # Too repetitive
            return 0.3
        elif diversity_ratio > 0.7:
            # Too random
            return 0.5
        else:
            return 0.7

    def _check_format_preservation(self, original: str, translated: str) -> float:
        """
        Check if formatting is preserved.

        Args:
            original: Original text
            translated: Translated text

        Returns:
            Quality score (0.0-1.0)
        """
        # Count format elements
        original_newlines = original.count("\n")
        translated_newlines = translated.count("\n")

        original_bullets = len(re.findall(r"^[\*\-•]\s", original, re.MULTILINE))
        translated_bullets = len(re.findall(r"^[\*\-•]\s", translated, re.MULTILINE))

        original_numbers = len(re.findall(r"^\d+\.\s", original, re.MULTILINE))
        translated_numbers = len(re.findall(r"^\d+\.\s", translated, re.MULTILINE))

        scores = []

        # Check newlines
        if original_newlines > 0:
            newline_score = 1.0 - abs(original_newlines - translated_newlines) / max(
                original_newlines, 1
            )
            scores.append(max(0.0, newline_score))

        # Check bullets
        if original_bullets > 0:
            bullet_score = 1.0 if original_bullets == translated_bullets else 0.5
            scores.append(bullet_score)

        # Check numbered lists
        if original_numbers > 0:
            number_score = 1.0 if original_numbers == translated_numbers else 0.5
            scores.append(number_score)

        if not scores:
            return 1.0  # No special formatting to preserve

        return sum(scores) / len(scores)

    def _check_special_characters(self, original: str, translated: str) -> float:
        """
        Check preservation of special characters.

        Args:
            original: Original text
            translated: Translated text

        Returns:
            Quality score (0.0-1.0)
        """
        # Extract numbers and special tokens
        original_numbers = re.findall(r"\d+(?:\.\d+)?", original)
        translated_numbers = re.findall(r"\d+(?:\.\d+)?", translated)

        original_emails = re.findall(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Z|a-z]{2,}\b", original)
        translated_emails = re.findall(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Z|a-z]{2,}\b", translated)

        original_urls = re.findall(r"https?://[^\s]+", original)
        translated_urls = re.findall(r"https?://[^\s]+", translated)

        scores = []

        # Check numbers preservation
        if original_numbers:
            numbers_preserved = len(set(original_numbers) & set(translated_numbers))
            number_score = numbers_preserved / len(original_numbers)
            scores.append(number_score)

        # Check email preservation
        if original_emails:
            emails_preserved = len(set(original_emails) & set(translated_emails))
            email_score = emails_preserved / len(original_emails)
            scores.append(email_score)

        # Check URL preservation
        if original_urls:
            urls_preserved = len(set(original_urls) & set(translated_urls))
            url_score = urls_preserved / len(original_urls)
            scores.append(url_score)

        if not scores:
            return 1.0  # No special characters to preserve

        return sum(scores) / len(scores)

    def validate_translation(
        self,
        original: str,
        translated: str,
        source_lang: str,
        target_lang: str,
    ) -> bool:
        """
        Validate if translation meets quality threshold.

        Args:
            original: Original text
            translated: Translated text
            source_lang: Source language code
            target_lang: Target language code

        Returns:
            True if translation passes quality check
        """
        quality = self.check_quality(original, translated, source_lang, target_lang)
        return quality["passed"]

    def suggest_improvements(
        self,
        original: str,
        translated: str,
    ) -> list[str]:
        """
        Suggest improvements for low-quality translation.

        Args:
            original: Original text
            translated: Translated text

        Returns:
            List of improvement suggestions
        """
        suggestions = []

        # Check length
        ratio = len(translated) / len(original) if original else 0
        if ratio < 0.5:
            suggestions.append("Translation appears too short. Check for missing content.")
        elif ratio > 2.5:
            suggestions.append("Translation appears too long. Check for added content.")

        # Check emptiness
        if not translated.strip():
            suggestions.append("Translation is empty.")

        # Check formatting
        if "\n" in original and "\n" not in translated:
            suggestions.append("Original formatting (line breaks) not preserved.")

        return suggestions


# Global quality checker instance
quality_checker = QualityChecker()
