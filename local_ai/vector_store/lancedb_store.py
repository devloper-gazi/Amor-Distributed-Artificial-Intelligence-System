"""
LanceDB Vector Storage with Nomic Embeddings
Serverless, embedded vector database for RAG
"""

import asyncio
import logging
from typing import List, Dict, Any, Optional
from pathlib import Path
from datetime import datetime
import hashlib

import lancedb
from lancedb.pydantic import LanceModel, Vector
from lancedb.embeddings import get_registry
from sentence_transformers import SentenceTransformer
import pyarrow as pa

logger = logging.getLogger(__name__)


class DocumentChunk(LanceModel):
    """Document chunk schema for LanceDB."""

    id: str
    text: str
    vector: Vector(768)  # nomic-embed-text-v1 produces 768-dim embeddings

    # Metadata
    document_id: str
    chunk_index: int
    source_url: Optional[str] = None
    title: Optional[str] = None
    language: Optional[str] = None
    created_at: str

    # Content metadata
    word_count: int
    char_count: int
    content_hash: str


class LanceDBVectorStore:
    """
    LanceDB vector storage with nomic-embed-text-v1 embeddings.
    Fully embedded, serverless vector database optimized for local deployment.
    """

    def __init__(
        self,
        db_path: str = "/data/vectors",
        embedding_model: str = "nomic-ai/nomic-embed-text-v1.5",
        table_name: str = "documents",
        device: str = "cpu",  # Use CPU to save VRAM for LLM
    ):
        """
        Initialize LanceDB vector store.

        Args:
            db_path: Path to LanceDB storage directory
            embedding_model: Sentence transformer model for embeddings
            table_name: Name of the LanceDB table
            device: Device for embeddings - 'cpu' or 'cuda'
        """
        self.db_path = Path(db_path)
        self.db_path.mkdir(parents=True, exist_ok=True)

        self.table_name = table_name
        self.device = device

        # Initialize LanceDB
        self.db = lancedb.connect(str(self.db_path))

        # Initialize embedding model (CPU-based to save VRAM)
        logger.info(f"Loading embedding model: {embedding_model} on {device}")
        self.embedding_model = SentenceTransformer(
            embedding_model,
            device=device,
        )

        # Set to 768 dimensions for nomic-embed
        self.embedding_dim = 768

        # Get or create table
        self.table = self._get_or_create_table()

        logger.info(f"LanceDB initialized at {db_path} with {embedding_model}")

    def _get_or_create_table(self) -> lancedb.table.Table:
        """Get existing table or create new one."""
        try:
            # Try to open existing table
            table = self.db.open_table(self.table_name)
            logger.info(f"Opened existing table: {self.table_name}")
            return table
        except Exception:
            # Create new table with schema
            logger.info(f"Creating new table: {self.table_name}")

            # Create empty table with schema
            schema = pa.schema([
                pa.field("id", pa.string()),
                pa.field("text", pa.string()),
                pa.field("vector", pa.list_(pa.float32(), 768)),
                pa.field("document_id", pa.string()),
                pa.field("chunk_index", pa.int64()),
                pa.field("source_url", pa.string()),
                pa.field("title", pa.string()),
                pa.field("language", pa.string()),
                pa.field("created_at", pa.string()),
                pa.field("word_count", pa.int64()),
                pa.field("char_count", pa.int64()),
                pa.field("content_hash", pa.string()),
            ])

            table = self.db.create_table(
                self.table_name,
                schema=schema,
            )

            return table

    async def _embed_text(self, text: str | List[str]) -> List[List[float]]:
        """
        Generate embeddings for text.

        Args:
            text: Single text or list of texts

        Returns:
            List of embedding vectors
        """
        # Run embedding in executor (sentence-transformers is synchronous)
        embeddings = await asyncio.get_event_loop().run_in_executor(
            None,
            lambda: self.embedding_model.encode(
                text if isinstance(text, list) else [text],
                normalize_embeddings=True,  # Normalize for cosine similarity
                show_progress_bar=False,
            )
        )

        return embeddings.tolist()

    def _chunk_text(
        self,
        text: str,
        chunk_size: int = 1000,
        chunk_overlap: int = 200,
    ) -> List[str]:
        """
        Chunk text into overlapping segments.

        Args:
            text: Text to chunk
            chunk_size: Target chunk size in characters
            chunk_overlap: Overlap between chunks

        Returns:
            List of text chunks
        """
        if len(text) <= chunk_size:
            return [text]

        chunks = []
        start = 0

        while start < len(text):
            end = start + chunk_size

            # Try to break at sentence boundary
            if end < len(text):
                # Look for sentence ending punctuation
                for punct in ['. ', '! ', '? ', '\n\n']:
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
        document_id: str,
        source_url: Optional[str] = None,
        title: Optional[str] = None,
        language: Optional[str] = None,
        chunk_size: int = 1000,
        chunk_overlap: int = 200,
    ) -> Dict[str, Any]:
        """
        Add document to vector store with chunking.

        Args:
            text: Document text
            document_id: Unique document identifier
            source_url: Source URL
            title: Document title
            language: Document language
            chunk_size: Chunk size in characters
            chunk_overlap: Overlap between chunks

        Returns:
            Dict with ingestion metadata
        """
        try:
            # Chunk text
            chunks = self._chunk_text(text, chunk_size, chunk_overlap)
            logger.info(f"Chunked document into {len(chunks)} chunks")

            # Generate embeddings for all chunks
            embeddings = await self._embed_text(chunks)

            # Create records
            records = []
            timestamp = datetime.utcnow().isoformat()

            for i, (chunk, embedding) in enumerate(zip(chunks, embeddings)):
                chunk_id = f"{document_id}_chunk_{i}"
                content_hash = hashlib.sha256(chunk.encode()).hexdigest()[:16]

                record = {
                    "id": chunk_id,
                    "text": chunk,
                    "vector": embedding,
                    "document_id": document_id,
                    "chunk_index": i,
                    "source_url": source_url or "",
                    "title": title or "",
                    "language": language or "",
                    "created_at": timestamp,
                    "word_count": len(chunk.split()),
                    "char_count": len(chunk),
                    "content_hash": content_hash,
                }
                records.append(record)

            # Add to LanceDB (synchronous operation)
            await asyncio.get_event_loop().run_in_executor(
                None,
                lambda: self.table.add(records)
            )

            logger.info(f"Added {len(chunks)} chunks to vector store")

            return {
                "success": True,
                "document_id": document_id,
                "chunks_created": len(chunks),
                "total_chars": len(text),
                "embedding_dim": self.embedding_dim,
            }

        except Exception as e:
            logger.error(f"Failed to add document: {e}")
            raise

    async def search(
        self,
        query: str,
        limit: int = 5,
        min_score: float = 0.5,
        filter_expr: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """
        Semantic search in vector store.

        Args:
            query: Search query
            limit: Maximum number of results
            min_score: Minimum similarity score (0-1)
            filter_expr: SQL-like filter expression

        Returns:
            List of search results with scores
        """
        try:
            # Generate query embedding
            query_embedding = await self._embed_text(query)
            query_vector = query_embedding[0]

            # Search in LanceDB
            search_results = await asyncio.get_event_loop().run_in_executor(
                None,
                lambda: self.table.search(query_vector)
                    .limit(limit * 2)  # Get more results for filtering
                    .to_list()
            )

            # Process and filter results
            results = []
            for result in search_results:
                # LanceDB returns distance, convert to similarity score
                # Assuming cosine distance (1 - cosine similarity)
                score = 1.0 - result.get("_distance", 1.0)

                if score >= min_score:
                    results.append({
                        "id": result.get("id"),
                        "text": result.get("text"),
                        "score": score,
                        "document_id": result.get("document_id"),
                        "chunk_index": result.get("chunk_index"),
                        "source_url": result.get("source_url"),
                        "title": result.get("title"),
                        "language": result.get("language"),
                        "word_count": result.get("word_count"),
                    })

            # Sort by score and limit
            results.sort(key=lambda x: x["score"], reverse=True)
            results = results[:limit]

            logger.info(f"Found {len(results)} results for query")
            return results

        except Exception as e:
            logger.error(f"Search failed: {e}")
            raise

    async def hybrid_search(
        self,
        query: str,
        limit: int = 5,
        vector_weight: float = 0.7,
        text_weight: float = 0.3,
    ) -> List[Dict[str, Any]]:
        """
        Hybrid search combining vector and text-based search.

        Args:
            query: Search query
            limit: Maximum number of results
            vector_weight: Weight for vector similarity
            text_weight: Weight for text matching

        Returns:
            List of search results with combined scores
        """
        # Get vector search results
        vector_results = await self.search(query, limit=limit * 2)

        # Simple keyword matching for text search
        query_terms = set(query.lower().split())

        # Combine and rerank
        for result in vector_results:
            text_lower = result["text"].lower()
            text_terms = set(text_lower.split())

            # Calculate text match score
            matches = len(query_terms.intersection(text_terms))
            text_score = min(1.0, matches / max(1, len(query_terms)))

            # Combine scores
            result["vector_score"] = result["score"]
            result["text_score"] = text_score
            result["score"] = (
                vector_weight * result["vector_score"] +
                text_weight * text_score
            )

        # Sort by combined score
        vector_results.sort(key=lambda x: x["score"], reverse=True)

        return vector_results[:limit]

    async def delete_document(self, document_id: str) -> Dict[str, Any]:
        """
        Delete all chunks of a document.

        Args:
            document_id: Document ID to delete

        Returns:
            Deletion metadata
        """
        try:
            # Delete using filter
            await asyncio.get_event_loop().run_in_executor(
                None,
                lambda: self.table.delete(f"document_id = '{document_id}'")
            )

            logger.info(f"Deleted document: {document_id}")
            return {"success": True, "document_id": document_id}

        except Exception as e:
            logger.error(f"Failed to delete document: {e}")
            raise

    async def get_stats(self) -> Dict[str, Any]:
        """Get vector store statistics."""
        try:
            count = await asyncio.get_event_loop().run_in_executor(
                None,
                lambda: self.table.count_rows()
            )

            return {
                "total_chunks": count,
                "embedding_dim": self.embedding_dim,
                "table_name": self.table_name,
                "db_path": str(self.db_path),
            }

        except Exception as e:
            logger.error(f"Failed to get stats: {e}")
            return {"error": str(e)}

    async def close(self):
        """Cleanup resources."""
        # LanceDB handles cleanup automatically
        logger.info("Vector store closed")