#!/usr/bin/env python3
"""Cycle UI 2026-05-20 — MongoDB chat_messages branching backfill.

The Unified Chat UI introduces a tree-shaped conversation model
(parent_id chains, current_leaf_id pointers) on top of the existing
flat `chat_messages` collection.  This script linearizes the pre-
Cycle-UI rows so the new readers (`get_active_branch` /
`$graphLookup` aggregation) return correct results from day one.

For each chat_session:
  1. Read messages sorted by ``created_at`` ascending.
  2. For each message, set ``parent_id`` = previous message's ``_id``
     (the first message keeps ``parent_id = None`` — it's the root).
  3. Set the session's ``current_leaf_id`` = last message's ``_id``.
  4. Backfill ``state = 'finished'``, ``event_log = []``,
     ``classifier_meta = None``, ``mode = session.mode``.

Idempotent: re-runs detect already-backfilled sessions via the
``cycle_ui_backfilled_at`` marker and skip them.

Usage::

    docker exec -e PYTHONPATH=/app amor-app-2 python -u \\
        /app/tools/migrations/2026_05_branching.py --dry-run
    # Then to apply:
    docker exec -e PYTHONPATH=/app amor-app-2 python -u \\
        /app/tools/migrations/2026_05_branching.py --apply

Rollback (irrecoverable for the parent_id chain, but the field can
be stripped to fall back to flat reads)::

    docker exec amor-mongo-1 mongosh amor --eval '
      db.chat_messages.updateMany({}, {$unset: {parent_id:1, state:1,
                                                event_log:1, mode:1,
                                                classifier_meta:1}});
      db.chat_sessions.updateMany({}, {$unset: {current_leaf_id:1,
                                                cycle_ui_backfilled_at:1}});
    '
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
from datetime import datetime, timezone
from typing import Any, Dict, List


def _print(msg: str) -> None:
    print(f"[CYCLE-UI-BACKFILL] {msg}", flush=True)


async def main_async(args: argparse.Namespace) -> int:
    # Defer heavy imports so `--help` is cheap.
    from document_processor.infrastructure.storage import storage_manager
    from document_processor.infrastructure.chat_store import chat_store

    # Connect Mongo + ensure new indexes exist (idempotent).
    await storage_manager.connect_mongo()
    db = storage_manager.mongo_db
    if db is None:
        _print("FATAL: MongoDB not initialized")
        return 2

    await chat_store.ensure_indexes()
    _print(f"indexes ensured (dry_run={args.dry_run})")

    sessions = db["chat_sessions"]
    messages = db["chat_messages"]

    total_sessions = await sessions.count_documents({})
    total_messages_pre = await messages.count_documents({})
    _print(f"pre: {total_sessions} sessions, {total_messages_pre} messages")

    # Already-backfilled sessions are skipped (idempotency).
    skip_filter = {"cycle_ui_backfilled_at": {"$exists": True}}
    already_done = await sessions.count_documents(skip_filter)
    _print(f"already backfilled: {already_done} sessions (will skip)")

    candidates_cursor = sessions.find(
        {"cycle_ui_backfilled_at": {"$exists": False}}
    )

    processed_sessions = 0
    processed_messages = 0
    failed_sessions: List[Dict[str, Any]] = []

    async for session in candidates_cursor:
        sid = session["_id"]
        session_mode = session.get("mode")
        try:
            msg_cursor = messages.find({"session_id": sid}).sort("created_at", 1)
            msgs = [m async for m in msg_cursor]
            if not msgs:
                # Empty session — just mark backfilled, nothing to chain.
                if not args.dry_run:
                    await sessions.update_one(
                        {"_id": sid},
                        {"$set": {
                            "current_leaf_id": None,
                            "cycle_ui_backfilled_at": datetime.now(timezone.utc),
                        }},
                    )
                processed_sessions += 1
                continue

            prev_id = None
            for m in msgs:
                mid = m["_id"]
                new_parent = prev_id  # None for the first message.
                # Only patch fields that haven't been set yet — preserve
                # any in-flight Cycle-UI writes that landed during the
                # backfill window.
                update: Dict[str, Any] = {}
                if "parent_id" not in m:
                    update["parent_id"] = new_parent
                if "state" not in m:
                    update["state"] = "finished"
                if "event_log" not in m:
                    update["event_log"] = []
                if "mode" not in m:
                    update["mode"] = session_mode
                if "classifier_meta" not in m:
                    update["classifier_meta"] = None
                if update:
                    if not args.dry_run:
                        await messages.update_one({"_id": mid}, {"$set": update})
                    processed_messages += 1
                prev_id = mid

            # Flip the session's current_leaf to the last message id +
            # mark as backfilled.
            leaf_id = msgs[-1]["_id"]
            if not args.dry_run:
                await sessions.update_one(
                    {"_id": sid},
                    {"$set": {
                        "current_leaf_id": leaf_id,
                        "cycle_ui_backfilled_at": datetime.now(timezone.utc),
                    }},
                )
            processed_sessions += 1

            if processed_sessions % 100 == 0:
                _print(
                    f"progress: {processed_sessions} sessions, "
                    f"{processed_messages} messages",
                )
        except Exception as exc:  # pragma: no cover
            failed_sessions.append({"session_id": sid, "error": str(exc)})
            _print(f"ERROR on session {sid}: {exc}")

    total_messages_post = await messages.count_documents({})
    _print(
        f"DONE — sessions={processed_sessions} messages_patched={processed_messages} "
        f"failed={len(failed_sessions)} "
        f"pre_msg_count={total_messages_pre} post_msg_count={total_messages_post}",
    )
    if total_messages_pre != total_messages_post:
        _print(
            "WARNING: message count changed during backfill "
            f"({total_messages_pre} → {total_messages_post}); "
            "concurrent writes likely — verify expected delta",
        )
    if failed_sessions:
        _print(f"FAILURES: {failed_sessions[:5]} (first 5 shown)")
        return 1
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__)
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--dry-run", action="store_true",
                   help="report what would change without writing")
    g.add_argument("--apply", action="store_true",
                   help="commit the backfill to MongoDB")
    return p


def main() -> int:
    args = build_parser().parse_args()
    # When --apply is set, --dry-run flips off so the runtime check below
    # uses a single bool.
    args.dry_run = not args.apply
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(message)s")
    return asyncio.run(main_async(args))


if __name__ == "__main__":
    raise SystemExit(main())
