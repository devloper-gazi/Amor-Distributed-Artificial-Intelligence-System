"""
LanceDB Vector Storage with Nomic Embeddings
Serverless, embedded vector database for RAG
"""

import asyncio
import logging
import time
from typing import List, Dict, Any, Optional
from pathlib import Path
from datetime import datetime, timezone
import hashlib

# Optional ML dependencies — Phase 16 lets the helper API (BM25
# scoring, RRF fusion, settings resolution) load without lancedb /
# sentence-transformers / pyarrow installed.  ``LanceDBVectorStore``
# constructor still requires them; ``__new__()`` does not.
#
# v18.1.5 — suppress the LanceDB-internal Pydantic v2 protected-
# namespace warning before it fires.  lancedb.pydantic.LanceModel
# declares a ``model_*`` field somewhere in its hierarchy without
# the ConfigDict(protected_namespaces=()) opt-out, which spams a
# UserWarning on first import.  This is third-party noise (upstream
# fix lives in lancedb >= a future release); the filter scope is
# tight enough that any AMOR-side `model_name` collision we
# accidentally introduce still surfaces.
import warnings as _warnings
_warnings.filterwarnings(
    "ignore",
    message=r'Field "model_name" has conflict with protected namespace "model_".*',
    category=UserWarning,
)
try:
    import lancedb
    from lancedb.pydantic import LanceModel, Vector
    from lancedb.embeddings import get_registry  # noqa: F401
    from sentence_transformers import SentenceTransformer
    import pyarrow as pa
    _HAS_VECTOR_DEPS = True
except ImportError:  # pragma: no cover - exercised only on minimal envs
    lancedb = None  # type: ignore[assignment]
    LanceModel = object  # type: ignore[assignment, misc]
    Vector = lambda _dim: None  # type: ignore[assignment, misc]
    SentenceTransformer = None  # type: ignore[assignment]
    pa = None  # type: ignore[assignment]
    _HAS_VECTOR_DEPS = False

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
        *,
        embedding_dim: Optional[int] = None,
        per_model_table: Optional[bool] = None,
    ):
        """
        Initialize LanceDB vector store.

        Args:
            db_path: Path to LanceDB storage directory
            embedding_model: Sentence transformer model for embeddings.
                Phase 16 — pass ``"BAAI/bge-m3"`` for the 1024-dim
                BGE-M3 embedder; ``per_model_table`` derives a fresh
                table so existing nomic-768 corpora stay untouched.
            table_name: Base table name.  Combined with the model slug
                + dim suffix when ``per_model_table`` is True.
            device: Device for embeddings - 'cpu' or 'cuda'
            embedding_dim: Override the auto-detected dimension.
                Defaults to whatever the model reports.
            per_model_table: When True, append a model-derived suffix
                to ``table_name`` so per-model corpora coexist.
                Defaults to ``settings.rag_per_model_table = True``.
        """
        if not _HAS_VECTOR_DEPS:
            raise ImportError(
                "LanceDBVectorStore requires lancedb + sentence-transformers; "
                "install via the production extras."
            )
        self.db_path = Path(db_path)
        self.db_path.mkdir(parents=True, exist_ok=True)

        self.device = device

        # Initialize LanceDB
        self.db = lancedb.connect(str(self.db_path))

        # Initialize embedding model (CPU-based to save VRAM)
        logger.info(f"Loading embedding model: {embedding_model} on {device}")
        self.embedding_model_name = embedding_model
        self.embedding_model = SentenceTransformer(
            embedding_model,
            device=device,
            trust_remote_code=True,  # nomic-embed + bge-m3 both want this
        )

        # Sniff the embedder's actual dimension; fall back to 768 if
        # the API doesn't expose it.  Override allowed for
        # off-the-shelf models with a documented dim.
        if embedding_dim is not None:
            self.embedding_dim = int(embedding_dim)
        else:
            try:
                self.embedding_dim = int(
                    self.embedding_model.get_sentence_embedding_dimension() or 768
                )
            except Exception:
                self.embedding_dim = 768

        # Resolve final table name.  Backwards-compat invariant: the
        # historical ``documents`` table on nomic-embed-text-v1.5 +
        # 768-dim is reached with default args.  Any *other* model
        # gets its own table (``documents_<slug>_<dim>``) so writes
        # never collide with the 768-dim schema.
        is_historical = (
            embedding_model == "nomic-ai/nomic-embed-text-v1.5"
            and self.embedding_dim == 768
        )
        if per_model_table is None:
            per_model_table = bool(self._settings_value(
                "rag_per_model_table", True,
            ))
        if (
            table_name == "documents"
            and per_model_table
            and not is_historical
        ):
            self.table_name = self._derive_table_name(
                table_name, embedding_model, self.embedding_dim,
            )
        else:
            self.table_name = table_name

        # Get or create table
        self.table = self._get_or_create_table()

        # Phase 16 — lazy cross-encoder reranker.  Only constructed
        # on the first ``rag_reranker_enabled`` request; ``None``
        # means "not yet initialised" or "init failed gracefully".
        self._reranker = None

        # Cycle H.0.2 — LazyGraphRAG knowledge-layer state.  Default
        # OFF (``settings.rag_graphrag_enabled=False``).  When enabled,
        # the search() path runs an entity-graph community pre-filter
        # before vector retrieval; without an index, falls through to
        # the legacy LanceDB-only path silently.  Build cost is one-
        # shot ~20-40 min on AMOR's ~50K-LOC corpus (Plan-agent risk
        # H.0.b), so the index is constructed via the admin /api/rag
        # endpoint or first-call (lazy) — see _ensure_lazy_graphrag_index.
        self._lazy_graphrag_index: Optional[Dict[str, Any]] = None
        self._lazy_graphrag_config = None

        logger.info(f"LanceDB initialized at {db_path} with {embedding_model}")

    @staticmethod
    def _derive_table_name(
        base: str, embedding_model: str, embedding_dim: int,
    ) -> str:
        """Build a per-model table name like
        ``documents_bge_m3_1024``.  Lowercases, replaces ``/`` and
        ``-`` with ``_``, strips the dot in version tags."""
        slug = embedding_model.split("/")[-1].lower()
        slug = slug.replace("-", "_").replace(".", "")
        return f"{base}_{slug}_{int(embedding_dim)}"

    def _get_or_create_table(self) -> "Any":  # lancedb.table.Table at runtime
        """Get existing table or create new one."""
        try:
            # Try to open existing table
            table = self.db.open_table(self.table_name)
            logger.info(f"Opened existing table: {self.table_name}")
            return table
        except Exception:
            # Create new table with schema
            logger.info(
                "Creating new table: %s (dim=%d)",
                self.table_name, self.embedding_dim,
            )

            # Create empty table with schema — vector dim follows
            # the embedder so BGE-M3 (1024) and nomic (768) get
            # their own correctly-shaped tables.
            schema = pa.schema([
                pa.field("id", pa.string()),
                pa.field("text", pa.string()),
                pa.field(
                    "vector",
                    pa.list_(pa.float32(), int(self.embedding_dim)),
                ),
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
        *,
        rerank: Optional[bool] = None,
        rerank_top_k: Optional[int] = None,
    ) -> List[Dict[str, Any]]:
        """
        Semantic search in vector store.

        Args:
            query: Search query
            limit: Maximum number of results
            min_score: Minimum similarity score (0-1)
            filter_expr: SQL-like filter expression
            rerank: Override the global ``rag_reranker_enabled``
                setting per-call.  ``None`` defers to settings.
            rerank_top_k: Maximum candidates to feed the reranker.
                Falls back to ``rag_reranker_top_k`` (default 20).

        Returns:
            List of search results with scores
        """
        try:
            # Generate query embedding
            query_embedding = await self._embed_text(query)
            query_vector = query_embedding[0]

            # Phase 16 — when reranker is enabled, ask LanceDB for a
            # wider candidate window so the cross-encoder has more to
            # work with.  Uses ``rag_reranker_top_k`` as the cap.
            do_rerank = self._should_rerank(rerank)
            wide_limit = max(
                limit * 2,
                self._reranker_top_k(rerank_top_k) if do_rerank else 0,
            )

            # Cycle H.0.2 — optional LazyGraphRAG community pre-filter.
            # When enabled AND the entity-graph index has been built,
            # narrow the candidate set to source_ids that belong to
            # communities sharing entities with the query.  This
            # reduces the dense-vector search space + improves
            # multi-hop precision (Microsoft 2024 measured 10-90%
            # cheaper than full GraphRAG; AMOR v20 gate condition #5
            # locks ≥15% nDCG@10 uplift on the 100-q bench).
            candidate_source_ids: Optional[set[str]] = None
            gr_config = self._load_lazy_graphrag_config()
            if (
                gr_config is not None
                and gr_config.enabled
                and self._lazy_graphrag_index is not None
            ):
                try:
                    candidate_source_ids = await self._lazy_graphrag_prefilter(
                        query, gr_config,
                    )
                except Exception as exc:  # pragma: no cover (defensive)
                    logger.debug(
                        "lazy_graphrag_prefilter failed; falling through to "
                        "LanceDB-only path: %s", exc,
                    )
                    candidate_source_ids = None

            # Search in LanceDB.  When a candidate set is present,
            # widen the limit so post-filtering doesn't starve the
            # dense ranking; LanceDB doesn't support set-IN filters
            # directly so we filter the hits in-process after the
            # ANN call returns (the candidate set is small relative
            # to corpus, so this is cheap).
            effective_wide = wide_limit
            if candidate_source_ids is not None:
                effective_wide = max(wide_limit * 3, 32)

            search_results = await asyncio.get_event_loop().run_in_executor(
                None,
                lambda: self.table.search(query_vector)
                    .limit(effective_wide)
                    .to_list()
            )
            if candidate_source_ids is not None:
                kept = [
                    r for r in search_results
                    if str(r.get("document_id") or r.get("source_id") or r.get("id") or "")
                       in candidate_source_ids
                ]
                # If the prefilter wipes everything (shouldn't happen
                # in practice, but defensive), keep the original
                # search results — never starve the user.
                search_results = kept or search_results

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

            # Sort by score, then optional cross-encoder rerank.
            results.sort(key=lambda x: x["score"], reverse=True)

            if do_rerank and results:
                results = await self._apply_reranker(query, results)

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
        *,
        rrf_k: Optional[int] = None,
        rerank: Optional[bool] = None,
    ) -> List[Dict[str, Any]]:
        """
        Hybrid search combining dense (vector) and sparse (BM25) retrieval
        with reciprocal rank fusion.

        Phase 16 — replaces the older keyword-overlap heuristic with the
        ``BM25`` implementation from
        ``document_processor.rag.rag_engine`` plus RRF over the dense
        and sparse rankings.  The cross-encoder reranker (when enabled)
        runs as a final pass before truncation.

        Args:
            query: Search query
            limit: Maximum number of results
            rrf_k: RRF constant (default 60, per the original RRF paper).
                Override per-call; defers to ``rag_rrf_k`` setting when
                ``None``.
            rerank: Override ``rag_reranker_enabled`` per-call.

        Returns:
            List of search results with combined ``score`` plus the
            individual ``vector_score`` / ``bm25_score`` /
            ``vector_rank`` / ``bm25_rank`` breakdown.
        """
        # Settings-driven master switch — when disabled, fall back to
        # plain dense search.  Default is True; a user with cached
        # benchmark snapshots can flip it off without a code change.
        if not self._hybrid_enabled():
            return await self.search(
                query, limit=limit, rerank=rerank,
            )

        # Wide candidate window so BM25 has signal.
        candidate_limit = max(limit * 4, 20)
        vector_results = await self.search(
            query, limit=candidate_limit, min_score=0.0, rerank=False,
        )
        if not vector_results:
            return []

        # BM25 over the candidate set.
        bm25_scores = self._bm25_scores(query, [r["text"] for r in vector_results])

        # Build (rank by dense, rank by sparse) → RRF.
        dense_ranking = {r["id"]: i for i, r in enumerate(vector_results)}
        sparse_indexed = sorted(
            enumerate(bm25_scores), key=lambda x: x[1], reverse=True,
        )
        sparse_ranking = {
            vector_results[idx]["id"]: rank
            for rank, (idx, _) in enumerate(sparse_indexed)
        }

        k = self._rrf_k(rrf_k)
        fused: List[Dict[str, Any]] = []
        for r in vector_results:
            doc_id = r["id"]
            d_rank = dense_ranking.get(doc_id, len(vector_results))
            s_rank = sparse_ranking.get(doc_id, len(vector_results))
            rrf = (1.0 / (k + d_rank + 1)) + (1.0 / (k + s_rank + 1))
            r2 = dict(r)
            r2["vector_score"] = r["score"]
            r2["bm25_score"] = bm25_scores[
                next(i for i, x in enumerate(vector_results) if x["id"] == doc_id)
            ]
            r2["vector_rank"] = d_rank
            r2["bm25_rank"] = s_rank
            r2["score"] = rrf
            fused.append(r2)

        fused.sort(key=lambda x: x["score"], reverse=True)

        if self._should_rerank(rerank) and fused:
            fused = await self._apply_reranker(query, fused)

        return fused[:limit]

    # ─── Phase 16 helpers ────────────────────────────────────────

    @staticmethod
    def _settings_value(name: str, default):
        try:
            from document_processor.config.settings import (  # noqa: PLC0415
                settings as _s,
            )
            return getattr(_s, name, default)
        except Exception:
            return default

    def _hybrid_enabled(self) -> bool:
        return bool(self._settings_value("rag_hybrid_search_enabled", True))

    def _should_rerank(self, override: Optional[bool]) -> bool:
        if override is not None:
            return bool(override)
        return bool(self._settings_value("rag_reranker_enabled", False))

    def _reranker_top_k(self, override: Optional[int]) -> int:
        if override is not None and override > 0:
            return int(override)
        v = self._settings_value("rag_reranker_top_k", 20)
        return int(v) if v else 20

    def _rrf_k(self, override: Optional[int]) -> int:
        if override is not None and override > 0:
            return int(override)
        v = self._settings_value("rag_rrf_k", 60)
        return int(v) if v else 60

    def _bm25_scores(self, query: str, texts: List[str]) -> List[float]:
        """Score every candidate against ``query`` using the BM25
        implementation from ``document_processor.rag.rag_engine``.
        Returns ``[0.0]*len(texts)`` if BM25 can't be imported (no
        ``document_processor`` on the path)."""
        if not texts:
            return []
        try:
            from document_processor.rag.rag_engine import BM25  # noqa: PLC0415
        except Exception as exc:  # pragma: no cover
            logger.debug("BM25 unavailable: %s", exc)
            return [0.0] * len(texts)
        try:
            k1 = float(self._settings_value("rag_bm25_k1", 1.5))
            b = float(self._settings_value("rag_bm25_b", 0.75))
            bm25 = BM25(k1=k1, b=b)
            bm25.fit(texts)
            return list(bm25.score(query))
        except Exception as exc:  # pragma: no cover
            logger.debug("BM25 scoring failed: %s", exc)
            return [0.0] * len(texts)

    async def _apply_reranker(
        self, query: str, results: List[Dict[str, Any]],
    ) -> List[Dict[str, Any]]:
        """Run a cross-encoder reranker over the candidate set.  When
        the optional ``sentence-transformers`` dependency is missing
        or initialisation fails, the original ordering is returned
        untouched (logged at debug level)."""
        if not results:
            return results
        try:
            from document_processor.rag.reranker import (  # noqa: PLC0415
                CrossEncoderReranker, RerankerConfig,
            )
        except Exception as exc:  # pragma: no cover
            logger.debug("reranker unavailable: %s", exc)
            return results
        if self._reranker is None:
            self._reranker = CrossEncoderReranker(RerankerConfig(device=self.device))
        try:
            await self._reranker.initialize()
        except Exception as exc:
            logger.debug("reranker initialise failed: %s", exc)
            self._reranker = None
            return results
        try:
            rer = await self._reranker.rerank(
                query, [r["text"] for r in results], top_k=None,
            )
        except Exception as exc:
            logger.debug("reranker run failed: %s", exc)
            return results
        # Map text→rerank score and reorder.
        score_by_text = {doc: score for doc, score in rer}
        for r in results:
            r["rerank_score"] = float(score_by_text.get(r["text"], 0.0))
        results.sort(key=lambda x: x.get("rerank_score", 0.0), reverse=True)
        return results

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

    # ─── Cycle H.0.2 — LazyGraphRAG knowledge-layer helpers ────────────

    def _load_lazy_graphrag_config(self):
        """Resolve LazyGraphRAGConfig from settings; cached on instance.

        Returns the dataclass or ``None`` when the module is unavailable
        or the import itself raises (keeps the LanceDB path resilient).
        """
        if self._lazy_graphrag_config is None:
            try:
                from document_processor.rag.lazy_graphrag import (  # noqa: PLC0415
                    load_config_from_settings,
                )
                self._lazy_graphrag_config = load_config_from_settings()
            except Exception as exc:
                logger.debug("lazy_graphrag config import failed: %s", exc)
                self._lazy_graphrag_config = None
        return self._lazy_graphrag_config

    async def _lazy_graphrag_prefilter(
        self,
        query: str,
        config,
    ) -> set[str]:
        """Return the set of source_ids belonging to communities whose
        top entities Jaccard-overlap with the query's entities.

        Uses ``filter_communities_by_relevance`` on the cached index
        (built via ``build_lazy_graphrag_index``).  Returns an empty
        set if the index is missing — caller falls through to the
        legacy LanceDB-only path.
        """
        if self._lazy_graphrag_index is None:
            return set()
        from document_processor.rag.lazy_graphrag import (  # noqa: PLC0415
            filter_communities_by_relevance,
        )
        communities = self._lazy_graphrag_index.get("communities") or []
        if not communities:
            return set()
        # Run the (CPU-bound) relevance ranking off the event loop so
        # we don't stall the FastAPI worker for big community sets.
        scored = await asyncio.get_event_loop().run_in_executor(
            None,
            lambda: filter_communities_by_relevance(
                query, communities,
                top_k=10,
                entity_min_length=config.entity_min_length,
            ),
        )
        candidate_ids: set[str] = set()
        for community, _score in scored:
            for sid in community.get("members") or []:
                candidate_ids.add(str(sid))
        return candidate_ids

    async def build_lazy_graphrag_index(
        self,
        chunks: Optional[List[Dict[str, Any]]] = None,
    ) -> Dict[str, Any]:
        """Build (or rebuild) the in-process LazyGraphRAG index from
        the current corpus.  When ``chunks`` is omitted, pulls them
        from the LanceDB table.  Caches the resulting
        ``{inv_index, communities, signature}`` on ``self`` so
        ``search()`` can use it.

        This is a heavy operation (~20-40 min on 50K chunks); the
        admin endpoint at ``POST /api/admin/rag/graphrag/build``
        runs it as a background task.  Returns an IndexStats payload.
        """
        from document_processor.rag.lazy_graphrag import (  # noqa: PLC0415
            build_entity_graph,
            detect_communities,
            stable_index_signature,
            IndexStats,
        )
        config = self._load_lazy_graphrag_config()
        if config is None:
            raise RuntimeError("lazy_graphrag module unavailable")

        # Pull the entire table as a chunk list when caller didn't
        # supply one.  At AMOR's hundreds-of-thousands chunk scale
        # this is a single LanceDB scan — accepted as a one-shot.
        if chunks is None:
            chunks = await asyncio.get_event_loop().run_in_executor(
                None,
                lambda: self.table.to_pandas().to_dict("records"),
            )

        t0 = time.time()
        inv_index = build_entity_graph(
            chunks, entity_min_length=config.entity_min_length,
        )
        communities = detect_communities(
            inv_index, min_size=config.community_min_size,
        )
        signature = stable_index_signature(chunks, config)

        elapsed = time.time() - t0
        self._lazy_graphrag_index = {
            "inv_index": inv_index,
            "communities": communities,
            "signature": signature,
            "built_at_utc": datetime.now(timezone.utc).isoformat(),
        }
        stats = IndexStats(
            documents_indexed=len(chunks),
            entities_extracted=len(inv_index),
            communities_detected=len(communities),
            wall_clock_s=elapsed,
        )
        logger.info(
            "lazy_graphrag.index_built chunks=%d entities=%d communities=%d "
            "elapsed=%.1fs",
            stats.documents_indexed, stats.entities_extracted,
            stats.communities_detected, stats.wall_clock_s,
        )
        return {
            "chunk_count": stats.documents_indexed,
            "entity_count": stats.entities_extracted,
            "community_count": stats.communities_detected,
            "build_duration_s": stats.wall_clock_s,
            "signature": signature,
        }