#!/usr/bin/env python3
"""Cycle H.2 — quick indexer for AMOR's own repo into LanceDB so the
LazyGraphRAG benchmark has a real corpus to run against.

The bench's seed file (``tests/eval/lazy_graphrag_100_questions.json``)
references ``relevant_source_ids`` as repo-relative paths like
``document_processor/code_intelligence/engine.py``.  This script
walks those paths, chunks them by line-budget, and adds the chunks
to the existing LanceDB ``documents`` table with ``document_id``
set to the relative path (so the bench's source-id matching works).

Usage::

    docker exec amor-app-2 python /app/tools/index_amor_for_graphrag.py \\
        --root /app --chunk-lines 80
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from pathlib import Path
from typing import List

if sys.platform == "win32":
    for _s in (sys.stdout, sys.stderr):
        _r = getattr(_s, "reconfigure", None)
        if _r is not None:
            try:
                _r(encoding="utf-8", errors="replace")
            except (OSError, ValueError):
                pass

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
logger = logging.getLogger(__name__)


_REPO_ROOT = Path(__file__).resolve().parent.parent

INDEX_DIRS = [
    "document_processor",
    "local_ai",
    "tools",
    "compose/llama-swap",
    "nginx",
]

INDEX_EXTS = {".py", ".yaml", ".yml", ".md", ".sh", ".conf", ".json"}

SKIP_PARTS = {"__pycache__", ".pytest_cache", "node_modules", ".git"}


def _walk_repo(root: Path) -> List[Path]:
    files: List[Path] = []
    for d in INDEX_DIRS:
        base = root / d
        if not base.is_dir():
            continue
        for p in base.rglob("*"):
            if not p.is_file():
                continue
            if p.suffix.lower() not in INDEX_EXTS:
                continue
            if any(part in SKIP_PARTS for part in p.parts):
                continue
            try:
                if p.stat().st_size > 200_000:
                    continue       # skip very large files
            except OSError:
                continue
            files.append(p)
    return files


def _chunk(text: str, lines_per_chunk: int = 80) -> List[str]:
    lines = text.splitlines()
    out: List[str] = []
    for i in range(0, len(lines), lines_per_chunk):
        chunk = "\n".join(lines[i:i + lines_per_chunk])
        if chunk.strip():
            out.append(chunk)
    return out


async def main_async(args: argparse.Namespace) -> int:
    from local_ai.vector_store.lancedb_store import LanceDBVectorStore  # noqa: PLC0415
    root = Path(args.root).resolve()
    print(f"[INDEXER] resolved root: {root}", flush=True)
    files = _walk_repo(root)
    print(f"[INDEXER] walked {len(files)} files", flush=True)
    if not files:
        print("[INDEXER] no files to index; exiting early", flush=True)
        return 1

    store = LanceDBVectorStore(db_path=args.db_path)
    pre = await store.get_stats()
    print(f"[INDEXER] LanceDB pre: {pre}", flush=True)

    total_chunks = 0
    for path in files:
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        rel = str(path.relative_to(root)).replace("\\", "/")
        # add_document takes raw text + chunks internally with size+overlap
        try:
            result = await store.add_document(
                text=text,
                document_id=rel,
                title=path.name,
                source_url=rel,
                language=path.suffix.lstrip("."),
                chunk_size=2000,
                chunk_overlap=200,
            )
            chunks_added = int(result.get("chunks_added", 0)) if isinstance(result, dict) else 1
            total_chunks += chunks_added
            if total_chunks // 200 != (total_chunks - chunks_added) // 200:
                print(f"[INDEXER] indexed {total_chunks} chunks (last: {rel})", flush=True)
        except Exception as exc:
            logger.warning("add_document failed for %s: %s", rel, exc)

    post = await store.get_stats()
    print(f"[INDEXER] LanceDB post: {post}", flush=True)
    print(f"[INDEXER] DONE: indexed {total_chunks} chunks total", flush=True)
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--root", default=str(_REPO_ROOT),
                   help="repo root to walk")
    p.add_argument("--db-path", default="/data/vectors",
                   help="LanceDB db_path (default /data/vectors)")
    p.add_argument("--chunk-lines", type=int, default=80,
                   help="lines per chunk (default 80)")
    return p


def main() -> int:
    return asyncio.run(main_async(build_parser().parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
