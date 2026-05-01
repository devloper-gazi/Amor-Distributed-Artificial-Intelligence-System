"""
Sentinel Evolution — Subsystem A: continuous preference logging.

Every user feedback (mark-as-true-positive / mark-as-false-positive
/ severity correction) becomes a ``PreferencePair`` recording the
``(input_context, agent_output_chosen, agent_output_rejected)``
triple in DPO format.

Storage:

* JSONL append-only log at ``preferences.jsonl`` — durable.
* SQLite indexed view at ``preferences.db`` — fast queries.

Privacy: by default the raw code snippet is NEVER stored.  Each
record carries a SHA-256 hash plus minimal structural features
(language, line count, AST shape proxy).  Opt-in raw mode is
gated by ``log_raw_code`` in the evolution config.

License: MIT.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import logging
import sqlite3
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Iterator, Literal


logger = logging.getLogger(__name__)


UserAction = Literal[
    "mark_true_positive",
    "mark_false_positive",
    "severity_correction",
    "ignore_finding",
    "promote_to_critical",
]


@dataclass
class PreferencePair:
    """One user feedback record in DPO-ready shape."""

    record_id: str = ""
    timestamp: float = 0.0
    scan_id: str = ""
    file_hash: str = ""           # sha256 of file path (stable per-user)
    line_range: str = ""          # "45-48"
    agent_name: str = ""          # auditor / reasoner / redteam / patcher / judge
    agent_prompt_version: str = ""
    agent_output_chosen: str = ""
    agent_output_rejected: str = ""
    user_action: UserAction = "mark_true_positive"
    cwe: str = ""
    severity_assigned: str = ""
    severity_user_corrected: str = ""
    # Privacy-preserving features (always populated; raw fields opt-in).
    code_hash: str = ""           # sha256 of code snippet
    language: str = ""
    token_count: int = 0
    ast_shape: str = ""           # rough fingerprint
    raw_snippet: str | None = None  # only when log_raw_code=True

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# ─────────────────────────────────────────────────────────────────────
# Privacy helpers
# ─────────────────────────────────────────────────────────────────────


def code_hash(snippet: str) -> str:
    """Stable, salt-free hash so two users who reviewed the same
    code produce the same fingerprint (no PII in the snippet)."""
    return hashlib.sha256((snippet or "").encode("utf-8")).hexdigest()[:32]


def file_hash(path: str) -> str:
    return hashlib.sha256((path or "").encode("utf-8")).hexdigest()[:32]


def ast_shape_proxy(snippet: str, *, max_chars: int = 8000) -> str:
    """Lightweight AST shape: keep only structural tokens
    (def/class/if/for/return/import/etc.).  Same code → same
    fingerprint regardless of identifier names — useful for
    "did the user reject this CWE pattern before?" queries.
    """
    if not snippet:
        return ""
    text = snippet[:max_chars]
    # Keep keywords + parens / colons / equals; collapse identifier
    # runs to a placeholder.
    keywords = (
        "def", "class", "if", "elif", "else", "for", "while", "return",
        "import", "from", "try", "except", "finally", "with", "yield",
        "raise", "lambda", "async", "await", "function", "var", "let",
        "const", "switch", "case", "throw", "catch", "static", "public",
        "private", "protected",
    )
    skeleton: list[str] = []
    in_word = False
    word = []
    for ch in text:
        if ch.isalnum() or ch == "_":
            word.append(ch)
            in_word = True
            continue
        if in_word:
            tok = "".join(word)
            word = []
            in_word = False
            skeleton.append(tok if tok in keywords else "_")
        if ch in "(){}[]:;.,=":
            skeleton.append(ch)
    if word:
        tok = "".join(word)
        skeleton.append(tok if tok in keywords else "_")
    return hashlib.sha256(
        "".join(skeleton).encode("utf-8"),
    ).hexdigest()[:24]


# ─────────────────────────────────────────────────────────────────────
# PreferenceStore
# ─────────────────────────────────────────────────────────────────────


_DB_LOCK = threading.Lock()


class PreferenceStore:
    """Append-only preference log with SQLite index."""

    JSONL_FILENAME = "preferences.jsonl"
    DB_FILENAME = "preferences.db"

    def __init__(self, root: str | Path, *, log_raw_code: bool = False) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.jsonl_path = self.root / self.JSONL_FILENAME
        self.db_path = self.root / self.DB_FILENAME
        self.log_raw_code = bool(log_raw_code)
        self._init_db()

    # ─── Lifecycle ─────────────────────────────────────────────

    def _init_db(self) -> None:
        with _DB_LOCK:
            con = sqlite3.connect(self.db_path)
            try:
                con.executescript(
                    """
                    CREATE TABLE IF NOT EXISTS preferences (
                        record_id            TEXT PRIMARY KEY,
                        timestamp            REAL NOT NULL,
                        scan_id              TEXT,
                        file_hash            TEXT,
                        line_range           TEXT,
                        agent_name           TEXT NOT NULL,
                        agent_prompt_version TEXT,
                        user_action          TEXT NOT NULL,
                        cwe                  TEXT,
                        severity_assigned    TEXT,
                        severity_user_corrected TEXT,
                        code_hash            TEXT,
                        language             TEXT,
                        token_count          INTEGER,
                        ast_shape            TEXT
                    );
                    CREATE INDEX IF NOT EXISTS idx_pref_agent ON preferences(agent_name);
                    CREATE INDEX IF NOT EXISTS idx_pref_cwe   ON preferences(cwe);
                    CREATE INDEX IF NOT EXISTS idx_pref_ts    ON preferences(timestamp);
                    """
                )
                con.commit()
            finally:
                con.close()

    # ─── Append ────────────────────────────────────────────────

    def record(
        self,
        *,
        scan_id: str,
        agent_name: str,
        user_action: UserAction,
        chosen: str,
        rejected: str = "",
        file: str = "",
        line_range: str = "",
        agent_prompt_version: str = "",
        cwe: str = "",
        severity_assigned: str = "",
        severity_user_corrected: str = "",
        snippet: str = "",
        language: str = "",
    ) -> PreferencePair:
        """Persist one feedback record.  Returns the constructed
        ``PreferencePair`` so callers can hand it to the ledger."""
        from uuid import uuid4
        pair = PreferencePair(
            record_id=uuid4().hex,
            timestamp=time.time(),
            scan_id=str(scan_id or ""),
            file_hash=file_hash(file),
            line_range=str(line_range or ""),
            agent_name=str(agent_name or ""),
            agent_prompt_version=str(agent_prompt_version or ""),
            agent_output_chosen=str(chosen or "")[:8000],
            agent_output_rejected=str(rejected or "")[:8000],
            user_action=user_action,
            cwe=str(cwe or ""),
            severity_assigned=str(severity_assigned or ""),
            severity_user_corrected=str(severity_user_corrected or ""),
            code_hash=code_hash(snippet),
            language=str(language or ""),
            token_count=len((snippet or "").split()),
            ast_shape=ast_shape_proxy(snippet),
            raw_snippet=snippet[:8000] if self.log_raw_code else None,
        )
        self._append_jsonl(pair)
        self._insert_db(pair)
        return pair

    def _append_jsonl(self, pair: PreferencePair) -> None:
        with _DB_LOCK:
            with self.jsonl_path.open("a", encoding="utf-8") as f:
                # raw_snippet is dropped from the on-disk log unless
                # opt-in is enabled.
                d = pair.to_dict()
                if not self.log_raw_code:
                    d["raw_snippet"] = None
                f.write(json.dumps(d, default=str, sort_keys=True))
                f.write("\n")

    def _insert_db(self, pair: PreferencePair) -> None:
        with _DB_LOCK:
            con = sqlite3.connect(self.db_path)
            try:
                con.execute(
                    """
                    INSERT OR REPLACE INTO preferences (
                        record_id, timestamp, scan_id, file_hash, line_range,
                        agent_name, agent_prompt_version, user_action, cwe,
                        severity_assigned, severity_user_corrected,
                        code_hash, language, token_count, ast_shape
                    ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        pair.record_id, pair.timestamp, pair.scan_id,
                        pair.file_hash, pair.line_range, pair.agent_name,
                        pair.agent_prompt_version, pair.user_action,
                        pair.cwe, pair.severity_assigned,
                        pair.severity_user_corrected, pair.code_hash,
                        pair.language, pair.token_count, pair.ast_shape,
                    ),
                )
                con.commit()
            finally:
                con.close()

    # ─── Read ──────────────────────────────────────────────────

    def count(self, *, agent_name: str | None = None) -> int:
        with _DB_LOCK:
            con = sqlite3.connect(self.db_path)
            try:
                if agent_name:
                    row = con.execute(
                        "SELECT COUNT(*) FROM preferences WHERE agent_name = ?",
                        (agent_name,),
                    ).fetchone()
                else:
                    row = con.execute(
                        "SELECT COUNT(*) FROM preferences",
                    ).fetchone()
                return int(row[0] if row else 0)
            finally:
                con.close()

    def by_agent(
        self,
        agent_name: str,
        *,
        limit: int = 1000,
    ) -> list[PreferencePair]:
        return self._read_jsonl(
            filter_fn=lambda d: d.get("agent_name") == agent_name,
            limit=limit,
        )

    def by_cwe(
        self,
        cwe: str,
        *,
        limit: int = 1000,
    ) -> list[PreferencePair]:
        return self._read_jsonl(
            filter_fn=lambda d: d.get("cwe") == cwe,
            limit=limit,
        )

    def all(self, *, limit: int = 5000) -> list[PreferencePair]:
        return self._read_jsonl(filter_fn=lambda _d: True, limit=limit)

    def _read_jsonl(
        self,
        *,
        filter_fn,
        limit: int,
    ) -> list[PreferencePair]:
        if not self.jsonl_path.is_file():
            return []
        out: list[PreferencePair] = []
        with self.jsonl_path.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    d = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not filter_fn(d):
                    continue
                out.append(PreferencePair(
                    record_id=str(d.get("record_id") or ""),
                    timestamp=float(d.get("timestamp") or 0.0),
                    scan_id=str(d.get("scan_id") or ""),
                    file_hash=str(d.get("file_hash") or ""),
                    line_range=str(d.get("line_range") or ""),
                    agent_name=str(d.get("agent_name") or ""),
                    agent_prompt_version=str(d.get("agent_prompt_version") or ""),
                    agent_output_chosen=str(d.get("agent_output_chosen") or ""),
                    agent_output_rejected=str(d.get("agent_output_rejected") or ""),
                    user_action=d.get("user_action") or "mark_true_positive",  # type: ignore[arg-type]
                    cwe=str(d.get("cwe") or ""),
                    severity_assigned=str(d.get("severity_assigned") or ""),
                    severity_user_corrected=str(d.get("severity_user_corrected") or ""),
                    code_hash=str(d.get("code_hash") or ""),
                    language=str(d.get("language") or ""),
                    token_count=int(d.get("token_count") or 0),
                    ast_shape=str(d.get("ast_shape") or ""),
                    raw_snippet=d.get("raw_snippet"),
                ))
                if len(out) >= limit:
                    break
        return out

    # ─── Convenience: DPO export ───────────────────────────────

    def export_dpo_dataset(
        self,
        *,
        agent_name: str,
        path: str | Path,
        max_pairs: int = 5000,
    ) -> int:
        """Write a JSONL file in `(prompt, chosen, rejected)` format
        ready for DPO fine-tuning.  Returns the row count."""
        pairs = self.by_agent(agent_name, limit=max_pairs)
        out_path = Path(path)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        rows = 0
        with out_path.open("w", encoding="utf-8") as f:
            for p in pairs:
                if not (p.agent_output_chosen and p.agent_output_rejected):
                    continue
                row = {
                    "prompt": (
                        f"agent={p.agent_name} cwe={p.cwe} "
                        f"language={p.language} ast={p.ast_shape}"
                    ),
                    "chosen": p.agent_output_chosen,
                    "rejected": p.agent_output_rejected,
                    "metadata": {
                        "scan_id": p.scan_id, "cwe": p.cwe,
                        "user_action": p.user_action,
                        "severity_assigned": p.severity_assigned,
                        "severity_user_corrected": p.severity_user_corrected,
                    },
                }
                f.write(json.dumps(row, default=str))
                f.write("\n")
                rows += 1
        return rows


__all__ = [
    "PreferencePair",
    "PreferenceStore",
    "UserAction",
    "ast_shape_proxy",
    "code_hash",
    "file_hash",
]
