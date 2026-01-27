"""
RAG Engine with Hybrid Search.

Production-grade Retrieval-Augmented Generation engine featuring:
- Hybrid search combining dense (vector) and sparse (BM25) retrieval
- Cross-encoder reranking for improved relevance
- Query decomposition for complex questions
- Context window optimization
- Source citation management
"""

import asyncio
import hashlib
import logging
import math
import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional, List, Dict, Any, Tuple
from collections import Counter

logger = logging.getLogger(__name__)

# Optional imports
try:
    from sentence_transformers import SentenceTransformer
    HAS_SENTENCE_TRANSFORMERS = True
except ImportError:
    HAS_SENTENCE_TRANSFORMERS = False
    logger.warning("sentence-transformers not installed. Vector search disabled.")

try:
    import numpy as np
    HAS_NUMPY = True
except ImportError:
    HAS_NUMPY = False


@dataclass
class RAGConfig:
    """Configuration for RAG engine."""
    # Embedding model
    embedding_model: str = "nomic-ai/nomic-embed-text-v1.5"
    embedding_dimension: int = 768
    embedding_device: str = "cpu"  # Save GPU for LLM
    
    # Search settings
    top_k: int = 10
    min_relevance_score: float = 0.3
    
    # Hybrid search weights
    dense_weight: float = 0.7
    sparse_weight: float = 0.3
    
    # Reranking
    use_reranker: bool = True
    reranker_model: str = "cross-encoder/ms-marco-MiniLM-L-6-v2"
    reranker_top_k: int = 5
    
    # Context settings
    max_context_tokens: int = 4000
    chunk_size: int = 500
    chunk_overlap: int = 100
    
    # Query processing
    decompose_complex_queries: bool = True
    max_sub_queries: int = 3
    
    # LLM settings (for synthesis)
    synthesis_model: str = "qwen2.5:7b"
    ollama_base_url: str = "http://localhost:11434"


@dataclass
class SearchResult:
    """A single search result."""
    id: str
    text: str
    score: float
    source_url: Optional[str] = None
    title: Optional[str] = None
    language: Optional[str] = None
    document_id: Optional[str] = None
    chunk_index: int = 0
    metadata: Dict[str, Any] = field(default_factory=dict)
    
    # Scoring breakdown
    dense_score: float = 0.0
    sparse_score: float = 0.0
    rerank_score: Optional[float] = None


@dataclass
class RAGResponse:
    """Response from RAG query."""
    answer: str
    sources: List[SearchResult]
    query: str
    sub_queries: List[str] = field(default_factory=list)
    confidence: float = 0.0
    response_time: float = 0.0
    metadata: Dict[str, Any] = field(default_factory=dict)


class BM25:
    """
    BM25 (Okapi BM25) sparse retrieval implementation.
    
    Formula: BM25(D,Q) = Σ IDF(qi) × (f(qi,D) × (k1+1)) / (f(qi,D) + k1 × (1-b+b×|D|/avgdl))
    """
    
    def __init__(self, k1: float = 1.5, b: float = 0.75):
        """
        Initialize BM25.
        
        Args:
            k1: Term frequency saturation parameter
            b: Length normalization parameter
        """
        self.k1 = k1
        self.b = b
        
        self.documents: List[List[str]] = []
        self.doc_lengths: List[int] = []
        self.avgdl: float = 0.0
        self.doc_freqs: Dict[str, int] = {}
        self.idf: Dict[str, float] = {}
        self.n_docs: int = 0
    
    def _tokenize(self, text: str) -> List[str]:
        """Simple tokenization."""
        # Lowercase and split on non-alphanumeric
        text = text.lower()
        tokens = re.findall(r'\b\w+\b', text)
        return tokens
    
    def fit(self, documents: List[str]):
        """
        Fit BM25 on a corpus of documents.
        
        Args:
            documents: List of document texts
        """
        self.documents = []
        self.doc_lengths = []
        self.doc_freqs = Counter()
        
        for doc in documents:
            tokens = self._tokenize(doc)
            self.documents.append(tokens)
            self.doc_lengths.append(len(tokens))
            
            # Count unique terms in document
            unique_terms = set(tokens)
            for term in unique_terms:
                self.doc_freqs[term] += 1
        
        self.n_docs = len(documents)
        self.avgdl = sum(self.doc_lengths) / self.n_docs if self.n_docs > 0 else 0
        
        # Calculate IDF for each term
        for term, df in self.doc_freqs.items():
            self.idf[term] = math.log(1 + (self.n_docs - df + 0.5) / (df + 0.5))
    
    def score(self, query: str) -> List[float]:
        """
        Score all documents against a query.
        
        Args:
            query: Query string
            
        Returns:
            List of BM25 scores for each document
        """
        query_tokens = self._tokenize(query)
        scores = []
        
        for i, doc_tokens in enumerate(self.documents):
            score = 0.0
            doc_len = self.doc_lengths[i]
            
            # Count term frequencies in document
            term_freqs = Counter(doc_tokens)
            
            for term in query_tokens:
                if term not in self.idf:
                    continue
                
                tf = term_freqs.get(term, 0)
                idf = self.idf[term]
                
                # BM25 formula
                numerator = tf * (self.k1 + 1)
                denominator = tf + self.k1 * (1 - self.b + self.b * doc_len / self.avgdl)
                
                score += idf * numerator / denominator
            
            scores.append(score)
        
        return scores
    
    def get_top_k(self, query: str, k: int = 10) -> List[Tuple[int, float]]:
        """
        Get top-k documents for a query.
        
        Returns:
            List of (doc_index, score) tuples
        """
        scores = self.score(query)
        
        # Get top-k indices
        indexed_scores = list(enumerate(scores))
        indexed_scores.sort(key=lambda x: x[1], reverse=True)
        
        return indexed_scores[:k]


class RAGEngine:
    """
    RAG Engine with hybrid search and reranking.
    
    Features:
    - Dense retrieval using sentence embeddings
    - Sparse retrieval using BM25
    - Hybrid search combining both approaches
    - Cross-encoder reranking for improved relevance
    - Query decomposition for complex questions
    - Context window optimization
    """
    
    def __init__(self, config: Optional[RAGConfig] = None):
        """
        Initialize RAG engine.
        
        Args:
            config: Engine configuration
        """
        self.config = config or RAGConfig()
        
        # Components
        self._embedding_model = None
        self._reranker = None
        self._bm25: Optional[BM25] = None
        
        # Document storage
        self._documents: List[Dict[str, Any]] = []
        self._embeddings: List[List[float]] = []
        
        self._initialized = False
    
    async def initialize(self):
        """Initialize the RAG engine."""
        if self._initialized:
            return
        
        logger.info("Initializing RAG engine...")
        
        # Load embedding model
        if HAS_SENTENCE_TRANSFORMERS:
            try:
                self._embedding_model = SentenceTransformer(
                    self.config.embedding_model,
                    device=self.config.embedding_device,
                )
                logger.info(f"Loaded embedding model: {self.config.embedding_model}")
            except Exception as e:
                logger.warning(f"Failed to load embedding model: {e}")
        
        # Load reranker
        if self.config.use_reranker:
            try:
                from .reranker import CrossEncoderReranker, RerankerConfig
                
                self._reranker = CrossEncoderReranker(
                    RerankerConfig(model_name=self.config.reranker_model)
                )
                await self._reranker.initialize()
                logger.info(f"Loaded reranker: {self.config.reranker_model}")
            except Exception as e:
                logger.warning(f"Failed to load reranker: {e}")
        
        self._initialized = True
        logger.info("RAG engine initialized")
    
    async def _embed_text(self, text: str) -> List[float]:
        """Generate embedding for text."""
        if not self._embedding_model:
            return [0.0] * self.config.embedding_dimension
        
        loop = asyncio.get_event_loop()
        embedding = await loop.run_in_executor(
            None,
            lambda: self._embedding_model.encode(text, normalize_embeddings=True)
        )
        
        return embedding.tolist()
    
    async def _embed_batch(self, texts: List[str]) -> List[List[float]]:
        """Generate embeddings for multiple texts."""
        if not self._embedding_model:
            return [[0.0] * self.config.embedding_dimension] * len(texts)
        
        loop = asyncio.get_event_loop()
        embeddings = await loop.run_in_executor(
            None,
            lambda: self._embedding_model.encode(
                texts,
                normalize_embeddings=True,
                show_progress_bar=False,
            )
        )
        
        return embeddings.tolist()
    
    def _chunk_text(
        self,
        text: str,
        chunk_size: Optional[int] = None,
        chunk_overlap: Optional[int] = None,
    ) -> List[str]:
        """Split text into overlapping chunks."""
        chunk_size = chunk_size or self.config.chunk_size
        chunk_overlap = chunk_overlap or self.config.chunk_overlap
        
        if len(text) <= chunk_size:
            return [text]
        
        chunks = []
        start = 0
        
        while start < len(text):
            end = start + chunk_size
            
            # Try to break at sentence boundary
            if end < len(text):
                for punct in ['. ', '! ', '? ', '\n\n', '\n']:
                    last_punct = text.rfind(punct, start, end)
                    if last_punct != -1:
                        end = last_punct + len(punct)
                        break
            
            chunk = text[start:end].strip()
            if chunk:
                chunks.append(chunk)
            
            start = end - chunk_overlap
        
        return chunks
    
    async def add_document(
        self,
        text: str,
        document_id: Optional[str] = None,
        source_url: Optional[str] = None,
        title: Optional[str] = None,
        language: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> int:
        """
        Add a document to the index.
        
        Args:
            text: Document text
            document_id: Optional document ID
            source_url: Source URL
            title: Document title
            language: Document language
            metadata: Additional metadata
            
        Returns:
            Number of chunks created
        """
        if not self._initialized:
            await self.initialize()
        
        # Generate document ID if not provided
        if not document_id:
            document_id = hashlib.sha256(text[:1000].encode()).hexdigest()[:16]
        
        # Chunk the document
        chunks = self._chunk_text(text)
        
        # Generate embeddings for all chunks
        embeddings = await self._embed_batch(chunks)
        
        # Store chunks
        for i, (chunk, embedding) in enumerate(zip(chunks, embeddings)):
            chunk_id = f"{document_id}_chunk_{i}"
            
            doc = {
                "id": chunk_id,
                "text": chunk,
                "document_id": document_id,
                "chunk_index": i,
                "source_url": source_url,
                "title": title,
                "language": language,
                "metadata": metadata or {},
                "created_at": datetime.utcnow().isoformat(),
            }
            
            self._documents.append(doc)
            self._embeddings.append(embedding)
        
        # Rebuild BM25 index
        self._build_bm25_index()
        
        logger.debug(f"Added document {document_id} with {len(chunks)} chunks")
        return len(chunks)
    
    async def add_documents(
        self,
        documents: List[Dict[str, Any]],
    ) -> int:
        """
        Add multiple documents.
        
        Args:
            documents: List of document dicts with 'text' and optional metadata
            
        Returns:
            Total number of chunks created
        """
        total_chunks = 0
        
        for doc in documents:
            chunks = await self.add_document(
                text=doc["text"],
                document_id=doc.get("id"),
                source_url=doc.get("source_url"),
                title=doc.get("title"),
                language=doc.get("language"),
                metadata=doc.get("metadata"),
            )
            total_chunks += chunks
        
        return total_chunks
    
    def _build_bm25_index(self):
        """Rebuild BM25 index from documents."""
        texts = [doc["text"] for doc in self._documents]
        self._bm25 = BM25()
        self._bm25.fit(texts)
    
    def _cosine_similarity(self, a: List[float], b: List[float]) -> float:
        """Calculate cosine similarity between two vectors."""
        if not HAS_NUMPY:
            # Fallback without numpy
            dot = sum(x * y for x, y in zip(a, b))
            norm_a = sum(x * x for x in a) ** 0.5
            norm_b = sum(x * x for x in b) ** 0.5
            if norm_a == 0 or norm_b == 0:
                return 0.0
            return dot / (norm_a * norm_b)
        
        a = np.array(a)
        b = np.array(b)
        return float(np.dot(a, b) / (np.linalg.norm(a) * np.linalg.norm(b)))
    
    async def _dense_search(
        self,
        query: str,
        top_k: int,
    ) -> List[Tuple[int, float]]:
        """
        Dense (vector) search.
        
        Args:
            query: Search query
            top_k: Number of results
            
        Returns:
            List of (doc_index, score) tuples
        """
        if not self._embedding_model or not self._embeddings:
            return []
        
        # Embed query
        query_embedding = await self._embed_text(query)
        
        # Calculate similarities
        similarities = []
        for i, doc_embedding in enumerate(self._embeddings):
            sim = self._cosine_similarity(query_embedding, doc_embedding)
            similarities.append((i, sim))
        
        # Sort by similarity
        similarities.sort(key=lambda x: x[1], reverse=True)
        
        return similarities[:top_k]
    
    async def _sparse_search(
        self,
        query: str,
        top_k: int,
    ) -> List[Tuple[int, float]]:
        """
        Sparse (BM25) search.
        
        Args:
            query: Search query
            top_k: Number of results
            
        Returns:
            List of (doc_index, score) tuples
        """
        if not self._bm25:
            return []
        
        return self._bm25.get_top_k(query, top_k)
    
    async def _hybrid_search(
        self,
        query: str,
        top_k: int,
    ) -> List[SearchResult]:
        """
        Hybrid search combining dense and sparse retrieval.
        
        Uses reciprocal rank fusion (RRF) or weighted combination.
        
        Args:
            query: Search query
            top_k: Number of results
            
        Returns:
            List of search results
        """
        # Get results from both methods
        dense_results = await self._dense_search(query, top_k * 2)
        sparse_results = await self._sparse_search(query, top_k * 2)
        
        # Normalize scores
        def normalize_scores(results: List[Tuple[int, float]]) -> Dict[int, float]:
            if not results:
                return {}
            max_score = max(r[1] for r in results) or 1.0
            return {idx: score / max_score for idx, score in results}
        
        dense_scores = normalize_scores(dense_results)
        sparse_scores = normalize_scores(sparse_results)
        
        # Combine scores
        all_indices = set(dense_scores.keys()) | set(sparse_scores.keys())
        combined_scores = {}
        
        for idx in all_indices:
            d_score = dense_scores.get(idx, 0.0)
            s_score = sparse_scores.get(idx, 0.0)
            
            combined_scores[idx] = (
                self.config.dense_weight * d_score +
                self.config.sparse_weight * s_score
            )
        
        # Sort by combined score
        sorted_indices = sorted(
            combined_scores.keys(),
            key=lambda x: combined_scores[x],
            reverse=True,
        )[:top_k]
        
        # Build results
        results = []
        for idx in sorted_indices:
            doc = self._documents[idx]
            
            result = SearchResult(
                id=doc["id"],
                text=doc["text"],
                score=combined_scores[idx],
                source_url=doc.get("source_url"),
                title=doc.get("title"),
                language=doc.get("language"),
                document_id=doc.get("document_id"),
                chunk_index=doc.get("chunk_index", 0),
                metadata=doc.get("metadata", {}),
                dense_score=dense_scores.get(idx, 0.0),
                sparse_score=sparse_scores.get(idx, 0.0),
            )
            results.append(result)
        
        return results
    
    async def _rerank(
        self,
        query: str,
        results: List[SearchResult],
        top_k: int,
    ) -> List[SearchResult]:
        """
        Rerank results using cross-encoder.
        
        Args:
            query: Original query
            results: Initial search results
            top_k: Number of results to return
            
        Returns:
            Reranked results
        """
        if not self._reranker or not results:
            return results[:top_k]
        
        # Rerank
        reranked = await self._reranker.rerank(
            query,
            [r.text for r in results],
            top_k=top_k,
        )
        
        # Update results with rerank scores
        for i, (text, score) in enumerate(reranked):
            for result in results:
                if result.text == text:
                    result.rerank_score = score
                    result.score = score  # Use rerank score as final score
                    break
        
        # Sort by rerank score
        results.sort(key=lambda x: x.rerank_score or 0.0, reverse=True)
        
        return results[:top_k]
    
    async def _decompose_query(self, query: str) -> List[str]:
        """
        Decompose a complex query into sub-queries.
        
        Args:
            query: Original query
            
        Returns:
            List of sub-queries
        """
        # Simple rule-based decomposition
        # For complex queries, could use LLM
        
        sub_queries = [query]
        
        # Split on conjunctions
        if " and " in query.lower():
            parts = re.split(r'\s+and\s+', query, flags=re.IGNORECASE)
            sub_queries.extend(parts)
        
        if " or " in query.lower():
            parts = re.split(r'\s+or\s+', query, flags=re.IGNORECASE)
            sub_queries.extend(parts)
        
        # Split questions
        if "?" in query:
            questions = [q.strip() + "?" for q in query.split("?") if q.strip()]
            sub_queries.extend(questions)
        
        # Deduplicate and limit
        seen = set()
        unique = []
        for q in sub_queries:
            if q.lower() not in seen and len(q) > 5:
                seen.add(q.lower())
                unique.append(q)
        
        return unique[:self.config.max_sub_queries]
    
    async def search(
        self,
        query: str,
        top_k: Optional[int] = None,
        filters: Optional[Dict[str, Any]] = None,
    ) -> List[SearchResult]:
        """
        Search the document index.
        
        Args:
            query: Search query
            top_k: Number of results (default from config)
            filters: Optional metadata filters
            
        Returns:
            List of search results
        """
        if not self._initialized:
            await self.initialize()
        
        top_k = top_k or self.config.top_k
        
        # Hybrid search
        results = await self._hybrid_search(query, top_k * 2)
        
        # Apply filters
        if filters:
            results = [
                r for r in results
                if all(r.metadata.get(k) == v for k, v in filters.items())
            ]
        
        # Filter by minimum score
        results = [r for r in results if r.score >= self.config.min_relevance_score]
        
        # Rerank if enabled
        if self.config.use_reranker:
            results = await self._rerank(query, results, top_k)
        else:
            results = results[:top_k]
        
        return results
    
    async def query(
        self,
        question: str,
        synthesize: bool = True,
    ) -> RAGResponse:
        """
        Answer a question using RAG.
        
        Args:
            question: Question to answer
            synthesize: Whether to synthesize an answer (requires LLM)
            
        Returns:
            RAG response with answer and sources
        """
        import time
        start_time = time.time()
        
        if not self._initialized:
            await self.initialize()
        
        # Decompose query if enabled
        sub_queries = []
        if self.config.decompose_complex_queries:
            sub_queries = await self._decompose_query(question)
        
        # Search for each sub-query
        all_results = []
        for query in sub_queries or [question]:
            results = await self.search(query)
            all_results.extend(results)
        
        # Deduplicate by document chunk
        seen_ids = set()
        unique_results = []
        for result in all_results:
            if result.id not in seen_ids:
                seen_ids.add(result.id)
                unique_results.append(result)
        
        # Sort by score and limit
        unique_results.sort(key=lambda x: x.score, reverse=True)
        top_results = unique_results[:self.config.reranker_top_k]
        
        # Synthesize answer if requested
        answer = ""
        confidence = 0.0
        
        if synthesize and top_results:
            answer, confidence = await self._synthesize_answer(question, top_results)
        else:
            # Just return context without synthesis
            answer = "\n\n---\n\n".join([
                f"**{r.title or 'Source'}**\n{r.text}" for r in top_results
            ])
            confidence = top_results[0].score if top_results else 0.0
        
        response_time = time.time() - start_time
        
        return RAGResponse(
            answer=answer,
            sources=top_results,
            query=question,
            sub_queries=sub_queries,
            confidence=confidence,
            response_time=response_time,
            metadata={
                "total_results": len(unique_results),
                "synthesized": synthesize,
            },
        )
    
    async def _synthesize_answer(
        self,
        question: str,
        sources: List[SearchResult],
    ) -> Tuple[str, float]:
        """
        Synthesize an answer from sources using LLM.
        
        Args:
            question: Original question
            sources: Relevant source documents
            
        Returns:
            (answer, confidence) tuple
        """
        try:
            import httpx
            
            # Build context from sources
            context_parts = []
            for i, source in enumerate(sources, 1):
                source_text = f"[Source {i}]"
                if source.title:
                    source_text += f" {source.title}"
                if source.source_url:
                    source_text += f" ({source.source_url})"
                source_text += f"\n{source.text}"
                context_parts.append(source_text)
            
            context = "\n\n".join(context_parts)
            
            # Truncate context if too long
            if len(context) > self.config.max_context_tokens * 4:
                context = context[:self.config.max_context_tokens * 4]
            
            # Create prompt
            prompt = f"""Based on the following sources, answer the question. Be concise and cite your sources using [Source N] notation.

Sources:
{context}

Question: {question}

Answer:"""
            
            # Call Ollama
            async with httpx.AsyncClient(timeout=60.0) as client:
                response = await client.post(
                    f"{self.config.ollama_base_url}/api/generate",
                    json={
                        "model": self.config.synthesis_model,
                        "prompt": prompt,
                        "stream": False,
                    },
                )
                
                if response.status_code == 200:
                    result = response.json()
                    answer = result.get("response", "").strip()
                    
                    # Calculate confidence based on source scores
                    avg_score = sum(s.score for s in sources) / len(sources)
                    
                    return answer, avg_score
                else:
                    logger.error(f"Ollama API error: {response.status_code}")
                    return "Failed to generate answer.", 0.0
                    
        except Exception as e:
            logger.error(f"Synthesis failed: {e}")
            return f"Error generating answer: {e}", 0.0
    
    def get_stats(self) -> Dict[str, Any]:
        """Get engine statistics."""
        return {
            "total_documents": len(set(d.get("document_id") for d in self._documents)),
            "total_chunks": len(self._documents),
            "embedding_dimension": self.config.embedding_dimension,
            "has_embeddings": len(self._embeddings) > 0,
            "has_bm25": self._bm25 is not None,
            "has_reranker": self._reranker is not None,
        }
    
    async def clear(self):
        """Clear all indexed documents."""
        self._documents = []
        self._embeddings = []
        self._bm25 = None
        logger.info("RAG engine cleared")
    
    async def close(self):
        """Cleanup resources."""
        if self._reranker:
            await self._reranker.close()
        logger.info("RAG engine closed")
    
    async def __aenter__(self):
        await self.initialize()
        return self
    
    async def __aexit__(self, exc_type, exc_val, exc_tb):
        await self.close()
