"""Unit tests for ``local_ai.vector_store.late_chunking`` and the
per-model table-name resolver — Phase 16 Commit D2.

Pure-Python tests; no LanceDB / sentence-transformers required.
"""

from __future__ import annotations

import pytest

from local_ai.vector_store.late_chunking import (
    LateChunk,
    LateChunker,
    iter_payloads,
)


# ─── derive_table_name (LanceDBVectorStore static helper) ─────────


def test_derive_table_name_for_nomic_default():
    from local_ai.vector_store.lancedb_store import LanceDBVectorStore

    name = LanceDBVectorStore._derive_table_name(
        "documents", "nomic-ai/nomic-embed-text-v1.5", 768,
    )
    assert name == "documents_nomic_embed_text_v15_768"


def test_derive_table_name_for_bge_m3():
    from local_ai.vector_store.lancedb_store import LanceDBVectorStore

    name = LanceDBVectorStore._derive_table_name(
        "documents", "BAAI/bge-m3", 1024,
    )
    assert name == "documents_bge_m3_1024"


def test_derive_table_name_handles_dot_and_dash():
    from local_ai.vector_store.lancedb_store import LanceDBVectorStore

    # snowflake/snowflake-arctic-embed-l-v2.0 → snowflake_arctic_embed_l_v20
    name = LanceDBVectorStore._derive_table_name(
        "documents", "Snowflake/snowflake-arctic-embed-l-v2.0", 1024,
    )
    assert name == "documents_snowflake_arctic_embed_l_v20_1024"


def test_derive_table_name_lowercases():
    from local_ai.vector_store.lancedb_store import LanceDBVectorStore

    name = LanceDBVectorStore._derive_table_name(
        "documents", "BAAI/BGE-M3", 1024,
    )
    assert name == "documents_bge_m3_1024"


# ─── LateChunker basics ───────────────────────────────────────────


def test_late_chunker_rejects_invalid_args():
    with pytest.raises(ValueError):
        LateChunker(chunk_size=0)
    with pytest.raises(ValueError):
        LateChunker(chunk_size=100, overlap=100)
    with pytest.raises(ValueError):
        LateChunker(chunk_size=100, overlap=-1)


def test_late_chunker_empty_input_returns_empty():
    chunker = LateChunker()
    assert chunker.chunk_with_context("") == []
    assert chunker.chunk_with_context("   \n\t  ") == []
    assert chunker.chunk_with_context(None) == []  # type: ignore[arg-type]


def test_late_chunker_short_text_single_chunk():
    text = "This is a short document. It has two sentences."
    chunker = LateChunker(chunk_size=1000, overlap=0)
    chunks = chunker.chunk_with_context(text)
    assert len(chunks) == 1
    c = chunks[0]
    assert c.text == text
    assert c.start == 0
    assert c.end == len(text)
    assert c.chunk_index == 0


def test_late_chunker_attaches_context():
    """Each chunk's ``contextual_payload`` must contain both the
    document context AND the chunk text."""
    text = (
        "Introduction paragraph about widgets. " * 50
    )
    chunker = LateChunker(chunk_size=200, overlap=20, window_chars=400)
    chunks = chunker.chunk_with_context(text)
    assert len(chunks) > 1
    for c in chunks:
        assert c.contextual_text  # window present
        assert c.text in c.contextual_payload  # chunk present
        assert c.contextual_text in c.contextual_payload  # context present


def test_late_chunker_context_is_capped_by_window_chars():
    text = "x" * 5000
    chunker = LateChunker(chunk_size=500, overlap=0, window_chars=1000)
    chunks = chunker.chunk_with_context(text)
    for c in chunks:
        assert len(c.contextual_text) <= 1000


def test_late_chunker_chunks_cover_input():
    text = "Sentence one. Sentence two. " * 20
    chunker = LateChunker(chunk_size=100, overlap=0, snap_to_sentence=False)
    chunks = chunker.chunk_with_context(text)
    # Concatenating chunk texts must reconstruct the original
    # (with no overlap).
    reconstructed = "".join(c.text for c in chunks)
    assert reconstructed == text


def test_late_chunker_overlap_respected():
    text = "abcdefghij" * 10  # 100 chars
    chunker = LateChunker(
        chunk_size=30, overlap=10, snap_to_sentence=False,
    )
    chunks = chunker.chunk_with_context(text)
    # Overlap means consecutive starts are 20 apart, not 30.
    starts = [c.start for c in chunks]
    diffs = [b - a for a, b in zip(starts, starts[1:])]
    assert all(d == 20 for d in diffs), diffs


def test_late_chunker_indexes_are_sequential():
    text = "a" * 1000
    chunker = LateChunker(chunk_size=200, overlap=0, snap_to_sentence=False)
    chunks = chunker.chunk_with_context(text)
    assert [c.chunk_index for c in chunks] == list(range(len(chunks)))


def test_iter_payloads_extracts_context_payloads():
    text = "Hello world. " * 50
    chunker = LateChunker(chunk_size=100, overlap=0)
    chunks = chunker.chunk_with_context(text)
    payloads = iter_payloads(chunks)
    assert len(payloads) == len(chunks)
    assert all(isinstance(p, str) for p in payloads)
    assert all(p == c.contextual_payload for p, c in zip(payloads, chunks))


def test_late_chunker_custom_template():
    chunker = LateChunker(
        chunk_size=1000,
        context_template="<doc>{context}</doc>\n<chunk>{chunk}</chunk>",
    )
    chunks = chunker.chunk_with_context("Hello world.")
    assert chunks[0].contextual_payload.startswith("<doc>Hello world.</doc>")
    assert "<chunk>Hello world.</chunk>" in chunks[0].contextual_payload
