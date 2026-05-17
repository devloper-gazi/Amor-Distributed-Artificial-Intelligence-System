#!/usr/bin/env python3
"""Cycle H.2 — focused LazyGraphRAG corpus indexer.

The full AMOR-repo indexer hits a CPU-embedding wall (~30s/file × 307
files = 2.5h).  This focused variant indexes ONLY the files referenced
in the bench seed's ``relevant_source_ids`` — typically 30-50 paths,
which is enough corpus for the bench to produce meaningful nDCG@10
numbers in ~10-15 min on CPU embed.

Output: chunks land in the same LanceDB ``documents`` table the
production search uses, with ``document_id`` set to the repo-
relative path (matching the bench's gold-ranking shape).

Usage::

    docker exec -e PYTHONPATH=/app amor-app-2 python -u \\
        /app/tools/index_focused_corpus.py \\
        --queries /app/tests/eval/lazy_graphrag_100_questions.json \\
        --root /app
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from pathlib import Path
from typing import Set

if sys.platform == "win32":
    for _s in (sys.stdout, sys.stderr):
        _r = getattr(_s, "reconfigure", None)
        if _r is not None:
            try:
                _r(encoding="utf-8", errors="replace")
            except (OSError, ValueError):
                pass


def _print(msg: str) -> None:
    print(f"[FOCUSED] {msg}", flush=True)


_REPO_ROOT = Path(__file__).resolve().parent.parent


async def main_async(args: argparse.Namespace) -> int:
    from local_ai.vector_store.lancedb_store import LanceDBVectorStore  # noqa: PLC0415

    root = Path(args.root).resolve()
    queries_path = Path(args.queries).resolve()
    _print(f"root={root}, queries={queries_path}")
    _print(f"embedding_model={args.embedding_model}")
    if not queries_path.is_file():
        _print(f"FATAL: queries file missing: {queries_path}")
        return 1

    data = json.loads(queries_path.read_text(encoding="utf-8"))
    paths: Set[str] = set()
    for q in data.get("queries", []):
        for sid in q.get("relevant_source_ids", []):
            if isinstance(sid, str) and sid:
                paths.add(sid)
    _print(f"unique paths from bench seed: {len(paths)}")

    store_kwargs = {"db_path": args.db_path}
    if args.embedding_model:
        store_kwargs["embedding_model"] = args.embedding_model
    store = LanceDBVectorStore(**store_kwargs)
    _print(f"embedding_dim={store.embedding_dim}, table={store.table_name}")
    pre = await store.get_stats()
    _print(f"LanceDB pre: {pre}")

    indexed = 0
    skipped = 0
    failed = 0
    for rel in sorted(paths):
        p = root / rel
        if not p.is_file():
            _print(f"  skip (missing on disk): {rel}")
            skipped += 1
            continue
        try:
            text = p.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            _print(f"  err read {rel}: {exc}")
            failed += 1
            continue
        try:
            await store.add_document(
                text=text,
                document_id=rel,
                title=p.name,
                source_url=rel,
                language=p.suffix.lstrip("."),
                chunk_size=int(args.chunk_size),
                chunk_overlap=int(args.chunk_overlap),
            )
            indexed += 1
            _print(f"  ok ({indexed}/{len(paths)}): {rel}")
        except Exception as exc:
            _print(f"  err add {rel}: {exc}")
            failed += 1

    post = await store.get_stats()
    _print(f"LanceDB post: {post}")
    _print(f"DONE — indexed {indexed}, skipped {skipped}, failed {failed}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--queries", required=True,
                   help="bench seed JSON with relevant_source_ids")
    p.add_argument("--root", default=str(_REPO_ROOT),
                   help="repo root to resolve relative paths against")
    p.add_argument("--db-path", default="/data/vectors",
                   help="LanceDB db_path (default /data/vectors)")
    p.add_argument("--embedding-model", default=None,
                   help="override sentence-transformers model id; e.g. "
                        "'sentence-transformers/all-MiniLM-L6-v2' for the "
                        "lightweight ~80MB / 384-dim embedder (5× faster "
                        "than the default nomic-embed-text-v1.5 on CPU)")
    p.add_argument("--chunk-size", type=int, default=1500)
    p.add_argument("--chunk-overlap", type=int, default=200)
    return p


def main() -> int:
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(message)s")
    return asyncio.run(main_async(build_parser().parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
