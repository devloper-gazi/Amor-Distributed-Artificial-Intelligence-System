"""
Base abstract processor for all source types.
Defines the interface that all source processors must implement.
"""

from abc import ABC, abstractmethod
from typing import AsyncIterator, Dict, Any
from ..core.models import SourceDocument


class BaseSourceProcessor(ABC):
    """
    Abstract base class for source processors.

    All source processors must inherit from this class and implement
    the required methods.
    """

    def __init__(self, settings):
        """
        Initialize base processor.

        Args:
            settings: Settings instance
        """
        self.settings = settings

    @abstractmethod
    async def can_process(self, source: SourceDocument) -> bool:
        """
        Check if this processor can handle the source.

        Args:
            source: Source document to check

        Returns:
            True if this processor can handle the source
        """
        pass

    @abstractmethod
    async def extract_content(self, source: SourceDocument) -> AsyncIterator[str]:
        """
        Extract content from source as async generator (streaming).

        This allows for memory-efficient processing of large sources
        by yielding content in chunks.

        Args:
            source: Source document to extract from

        Yields:
            Content chunks as strings
        """
        pass

    @abstractmethod
    async def get_metadata(self, source: SourceDocument) -> Dict[str, Any]:
        """
        Extract metadata from source.

        Args:
            source: Source document

        Returns:
            Dictionary with metadata
        """
        pass

    async def cleanup(self):
        """
        Cleanup resources (override if needed).

        Called when processor is done processing.
        """
        pass

    async def __aenter__(self):
        """Async context manager entry."""
        return self

    async def __aexit__(self, *args):
        """Async context manager exit."""
        await self.cleanup()
