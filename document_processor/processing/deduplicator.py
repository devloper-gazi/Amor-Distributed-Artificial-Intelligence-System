"""
Deduplication using Bloom filter for memory-efficient duplicate detection.
Handles both probabilistic and exact deduplication.
"""

import hashlib
from typing import Optional
from pybloom_live import BloomFilter
from ..config.settings import settings
from ..config.logging_config import logger
from ..core.exceptions import DeduplicationError


class Deduplicator:
    """
    Document deduplicator using Bloom filter.

    Provides memory-efficient duplicate detection with configurable
    false positive rate.
    """

    def __init__(
        self,
        capacity: Optional[int] = None,
        error_rate: Optional[float] = None,
        use_exact: bool = False,
    ):
        """
        Initialize deduplicator.

        Args:
            capacity: Expected number of unique items
            error_rate: Acceptable false positive rate
            use_exact: Use exact deduplication (slower, no false positives)
        """
        self.capacity = capacity or settings.bloom_filter_capacity
        self.error_rate = error_rate or settings.bloom_filter_error_rate
        self.use_exact = use_exact

        # Initialize Bloom filter for probabilistic deduplication
        try:
            self.bloom = BloomFilter(
                capacity=self.capacity,
                error_rate=self.error_rate,
            )
        except Exception as e:
            logger.error("bloom_filter_init_failed", error=str(e))
            raise DeduplicationError(f"Failed to initialize Bloom filter: {e}")

        # Set for exact deduplication (when needed)
        self.exact_hashes = set() if use_exact else None

        # Statistics
        self.seen_count = 0
        self.duplicate_count = 0
        self.unique_count = 0

        logger.info(
            "deduplicator_initialized",
            capacity=self.capacity,
            error_rate=self.error_rate,
            use_exact=use_exact,
        )

    def _compute_hash(self, content: str, algorithm: str = "sha256") -> str:
        """
        Compute hash of content.

        Args:
            content: Content to hash
            algorithm: Hash algorithm

        Returns:
            Hash digest
        """
        if algorithm == "md5":
            return hashlib.md5(content.encode("utf-8")).hexdigest()
        elif algorithm == "sha1":
            return hashlib.sha1(content.encode("utf-8")).hexdigest()
        else:
            return hashlib.sha256(content.encode("utf-8")).hexdigest()

    def is_duplicate(
        self,
        content: str,
        exact: Optional[bool] = None,
    ) -> bool:
        """
        Check if content is a duplicate.

        Args:
            content: Content to check
            exact: Override use_exact setting for this check

        Returns:
            True if duplicate (or probably duplicate for Bloom filter)
        """
        self.seen_count += 1

        # Compute content hash
        content_hash = self._compute_hash(content)

        # Use exact deduplication if requested
        if exact or (exact is None and self.use_exact):
            if content_hash in self.exact_hashes:
                self.duplicate_count += 1
                logger.debug(
                    "duplicate_detected_exact",
                    hash=content_hash[:16],
                )
                return True

            self.exact_hashes.add(content_hash)
            self.unique_count += 1
            return False

        # Use probabilistic Bloom filter
        if content_hash in self.bloom:
            # Probably a duplicate (small chance of false positive)
            self.duplicate_count += 1
            logger.debug(
                "duplicate_detected_bloom",
                hash=content_hash[:16],
                false_positive_possible=True,
            )
            return True

        # Definitely not a duplicate
        self.bloom.add(content_hash)
        self.unique_count += 1
        return False

    def add(self, content: str):
        """
        Add content to deduplicator without checking.

        Args:
            content: Content to add
        """
        content_hash = self._compute_hash(content)

        if self.use_exact:
            self.exact_hashes.add(content_hash)
        else:
            self.bloom.add(content_hash)

        self.unique_count += 1
        self.seen_count += 1

    def contains(self, content: str) -> bool:
        """
        Check if content exists (without updating counters).

        Args:
            content: Content to check

        Returns:
            True if content exists
        """
        content_hash = self._compute_hash(content)

        if self.use_exact:
            return content_hash in self.exact_hashes
        else:
            return content_hash in self.bloom

    def get_stats(self) -> dict:
        """
        Get deduplication statistics.

        Returns:
            Dictionary with statistics
        """
        duplicate_rate = (
            (self.duplicate_count / self.seen_count * 100)
            if self.seen_count > 0
            else 0.0
        )

        stats = {
            "seen": self.seen_count,
            "duplicates": self.duplicate_count,
            "unique": self.unique_count,
            "duplicate_rate": duplicate_rate,
            "use_exact": self.use_exact,
        }

        if not self.use_exact:
            stats.update({
                "bloom_capacity": self.bloom.capacity,
                "bloom_count": self.bloom.count,
                "bloom_error_rate": self.error_rate,
                "bloom_fill_rate": (self.bloom.count / self.bloom.capacity * 100),
            })
        else:
            stats["exact_hashes"] = len(self.exact_hashes)

        return stats

    def clear(self):
        """Clear deduplicator state."""
        # Reinitialize Bloom filter
        self.bloom = BloomFilter(
            capacity=self.capacity,
            error_rate=self.error_rate,
        )

        if self.exact_hashes is not None:
            self.exact_hashes.clear()

        # Reset counters
        self.seen_count = 0
        self.duplicate_count = 0
        self.unique_count = 0

        logger.info("deduplicator_cleared")

    def get_memory_usage(self) -> dict:
        """
        Estimate memory usage.

        Returns:
            Dictionary with memory usage information
        """
        import sys

        memory = {
            "bloom_filter_bytes": sys.getsizeof(self.bloom),
        }

        if self.exact_hashes is not None:
            memory["exact_hashes_bytes"] = sys.getsizeof(self.exact_hashes)

        memory["total_bytes"] = sum(memory.values())
        memory["total_mb"] = memory["total_bytes"] / (1024 * 1024)

        return memory


class SimilarityDeduplicator:
    """
    Advanced deduplicator using content similarity.

    Uses simhash or minhash for near-duplicate detection.
    """

    def __init__(self, similarity_threshold: float = 0.9):
        """
        Initialize similarity deduplicator.

        Args:
            similarity_threshold: Similarity threshold (0.0-1.0)
        """
        self.similarity_threshold = similarity_threshold
        self.documents = {}  # hash -> content
        self.seen_count = 0
        self.duplicate_count = 0

    def _compute_simhash(self, content: str, hash_bits: int = 64) -> int:
        """
        Compute simhash of content.

        Args:
            content: Content to hash
            hash_bits: Number of hash bits

        Returns:
            Simhash value
        """
        # Simple simhash implementation
        tokens = content.lower().split()
        vector = [0] * hash_bits

        for token in tokens:
            token_hash = int(hashlib.md5(token.encode()).hexdigest(), 16)

            for i in range(hash_bits):
                if token_hash & (1 << i):
                    vector[i] += 1
                else:
                    vector[i] -= 1

        fingerprint = 0
        for i in range(hash_bits):
            if vector[i] > 0:
                fingerprint |= (1 << i)

        return fingerprint

    def _hamming_distance(self, hash1: int, hash2: int) -> int:
        """Calculate Hamming distance between two hashes."""
        xor = hash1 ^ hash2
        distance = 0

        while xor:
            distance += 1
            xor &= xor - 1

        return distance

    def _similarity(self, hash1: int, hash2: int, hash_bits: int = 64) -> float:
        """Calculate similarity between two hashes."""
        distance = self._hamming_distance(hash1, hash2)
        return 1.0 - (distance / hash_bits)

    def is_duplicate(self, content: str) -> bool:
        """
        Check if content is similar to existing content.

        Args:
            content: Content to check

        Returns:
            True if similar content exists
        """
        self.seen_count += 1

        # Compute simhash
        content_hash = self._compute_simhash(content)

        # Check similarity with existing documents
        for existing_hash in self.documents:
            similarity = self._similarity(content_hash, existing_hash)

            if similarity >= self.similarity_threshold:
                self.duplicate_count += 1
                logger.debug(
                    "similar_duplicate_detected",
                    similarity=similarity,
                )
                return True

        # Not a duplicate - add to collection
        self.documents[content_hash] = content[:100]  # Store sample
        return False

    def get_stats(self) -> dict:
        """Get statistics."""
        return {
            "seen": self.seen_count,
            "duplicates": self.duplicate_count,
            "unique": len(self.documents),
            "similarity_threshold": self.similarity_threshold,
        }


# Global deduplicator instance
deduplicator = Deduplicator()
