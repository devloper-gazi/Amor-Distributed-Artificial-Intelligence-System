"""
RAG (Retrieval-Augmented Generation) module.
Provides hybrid search and reranking for research synthesis.
"""

from .rag_engine import RAGEngine, RAGConfig, SearchResult, RAGResponse
from .reranker import CrossEncoderReranker, RerankerConfig

__all__ = [
    "RAGEngine",
    "RAGConfig",
    "SearchResult",
    "RAGResponse",
    "CrossEncoderReranker",
    "RerankerConfig",
]
