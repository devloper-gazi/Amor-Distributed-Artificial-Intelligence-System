"""
Cross-Encoder Reranker for RAG.

Provides relevance reranking using cross-encoder models
to improve search result quality.
"""

import asyncio
import logging
from dataclasses import dataclass
from typing import Optional, List, Tuple

logger = logging.getLogger(__name__)

# Optional imports
try:
    from sentence_transformers import CrossEncoder
    HAS_CROSS_ENCODER = True
except ImportError:
    HAS_CROSS_ENCODER = False
    logger.warning("sentence-transformers not installed. Reranking disabled.")


@dataclass
class RerankerConfig:
    """Configuration for cross-encoder reranker."""
    model_name: str = "cross-encoder/ms-marco-MiniLM-L-6-v2"
    device: str = "cpu"
    max_length: int = 512
    batch_size: int = 32


class CrossEncoderReranker:
    """
    Cross-encoder based reranker for improving search relevance.
    
    Uses a cross-encoder model to score query-document pairs,
    which typically provides better relevance scores than
    bi-encoder similarity.
    
    Popular models:
    - cross-encoder/ms-marco-MiniLM-L-6-v2 (fast, good quality)
    - cross-encoder/ms-marco-MiniLM-L-12-v2 (slower, better quality)
    - cross-encoder/ms-marco-TinyBERT-L-2-v2 (fastest)
    """
    
    def __init__(self, config: Optional[RerankerConfig] = None):
        """
        Initialize reranker.
        
        Args:
            config: Reranker configuration
        """
        self.config = config or RerankerConfig()
        self._model = None
        self._initialized = False
    
    async def initialize(self):
        """Initialize the reranker model."""
        if self._initialized:
            return
        
        if not HAS_CROSS_ENCODER:
            logger.warning("Cross-encoder not available")
            return
        
        try:
            logger.info(f"Loading reranker model: {self.config.model_name}")
            
            # Load model in executor (blocking operation)
            loop = asyncio.get_event_loop()
            self._model = await loop.run_in_executor(
                None,
                lambda: CrossEncoder(
                    self.config.model_name,
                    max_length=self.config.max_length,
                    device=self.config.device,
                )
            )
            
            self._initialized = True
            logger.info("Reranker initialized successfully")
            
        except Exception as e:
            logger.error(f"Failed to initialize reranker: {e}")
            raise
    
    async def rerank(
        self,
        query: str,
        documents: List[str],
        top_k: Optional[int] = None,
    ) -> List[Tuple[str, float]]:
        """
        Rerank documents by relevance to query.
        
        Args:
            query: Search query
            documents: List of document texts
            top_k: Number of top results to return (None = all)
            
        Returns:
            List of (document, score) tuples sorted by relevance
        """
        if not self._initialized:
            await self.initialize()
        
        if not self._model or not documents:
            # Return original order with dummy scores
            return [(doc, 1.0 / (i + 1)) for i, doc in enumerate(documents)]
        
        try:
            # Create query-document pairs
            pairs = [(query, doc) for doc in documents]
            
            # Score pairs
            loop = asyncio.get_event_loop()
            scores = await loop.run_in_executor(
                None,
                lambda: self._model.predict(
                    pairs,
                    batch_size=self.config.batch_size,
                    show_progress_bar=False,
                )
            )
            
            # Pair documents with scores
            doc_scores = list(zip(documents, scores.tolist()))
            
            # Sort by score descending
            doc_scores.sort(key=lambda x: x[1], reverse=True)
            
            # Apply top_k
            if top_k is not None:
                doc_scores = doc_scores[:top_k]
            
            return doc_scores
            
        except Exception as e:
            logger.error(f"Reranking failed: {e}")
            # Return original order as fallback
            return [(doc, 1.0 / (i + 1)) for i, doc in enumerate(documents[:top_k])]
    
    async def score_pair(self, query: str, document: str) -> float:
        """
        Score a single query-document pair.
        
        Args:
            query: Search query
            document: Document text
            
        Returns:
            Relevance score
        """
        if not self._initialized:
            await self.initialize()
        
        if not self._model:
            return 0.0
        
        try:
            loop = asyncio.get_event_loop()
            score = await loop.run_in_executor(
                None,
                lambda: self._model.predict([(query, document)])[0]
            )
            return float(score)
            
        except Exception as e:
            logger.error(f"Scoring failed: {e}")
            return 0.0
    
    async def close(self):
        """Cleanup resources."""
        if self._model:
            del self._model
            self._model = None
        self._initialized = False
        logger.info("Reranker closed")
    
    @property
    def is_available(self) -> bool:
        """Check if reranker is available."""
        return HAS_CROSS_ENCODER and self._initialized
