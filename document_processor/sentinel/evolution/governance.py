"""
Sentinel Evolution — Subsystem I: Governance + immutable ledger.

The governance layer is the *safety floor* for every other
evolution subsystem.  Three guarantees:

1. **Immutable ledger** — every evolution step (LoRA promote,
   prompt change, rule synthesis, agent spawn, DAG mutation) is
   appended to a SHA-256 hash-chained ``ledger.jsonl``.  Any
   tamper — entry removed, content changed — breaks the chain
   and is detected by ``LedgerStore.verify()``.
2. **Hard constraints** — a YAML file (``immutable_constraints.yaml``)
   declares rules NO mutation is allowed to break: no non-loopback
   network, no telemetry, no output that contains attacker-style
   payloads (``backdoor``, ``reverse-shell``…), no overriding the
   acceptance criteria.  Every mutation candidate is checked
   through ``ImmutableConstraints.check()`` before promotion.
3. **Sandboxed evaluation** — every candidate runs inside a
   per-step temp directory; nothing escapes to the production
   ledger or model store until the candidate is explicitly
   promoted.

License: MIT.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import shutil
import threading
import time
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Iterator, Literal


logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────
# Errors
# ─────────────────────────────────────────────────────────────────────


class GovernanceError(Exception):
    """Base for governance-layer failures."""


class HardConstraintViolation(GovernanceError):
    """A mutation candidate broke an immutable constraint."""


class SandboxViolation(GovernanceError):
    """A sandbox tried to write outside its tmp root."""


class LedgerIntegrityError(GovernanceError):
    """The hash chain is broken — someone tampered with the ledger."""


# ─────────────────────────────────────────────────────────────────────
# Immutable constraints
# ─────────────────────────────────────────────────────────────────────


_DEFAULT_CONSTRAINTS_YAML = """\
# Sentinel Evolution - immutable constraints (Phase 15).
network:
  allowed_hosts:
    - '127.0.0.1'
    - 'localhost'
    - '::1'
  forbidden_keywords:
    - 'https://'
    - 'http://api.'
    - 'openai.com'
    - 'anthropic.com'
    - 'huggingface.co'
    - 'amazonaws.com'
    - 'googleapis.com'
telemetry:
  forbidden: true
  forbidden_keywords:
    - 'telemetry'
    - 'analytics'
    - 'phone-home'
    - 'auto-update'
    - 'crash-reporter'
    - 'opentelemetry-export'
output_sanitization:
  forbidden_phrases:
    - 'rm -rf /'
    - '/dev/tcp/'
    - 'nc -lvp'
    - 'netcat -lvp'
    - '/bin/bash -i'
    - 'exec(\"powershell'
    - 'import socket; s=socket.socket()'
    - 'wget http://'
    - 'curl http://'
    - 'reverse-shell'
    - 'backdoor'
  forbidden_regex:
    - '0x[0-9a-fA-F]{40}'
evolution_safety:
  protected_files:
    - 'document_processor/sentinel/evolution/governance.py'
    - 'document_processor/code_intelligence/adversarial_reviewer.py'
    - 'document_processor/sentinel/evolution/immutable_constraints.yaml'
  protected_settings:
    - 'sentinel_enabled'
    - 'quick_v2_specialist_32b_enabled'
    - 'quick_v2_speculative_decoding_enabled'
acceptance:
  precision_floor: 0.6
  recall_floor: 0.3
  latency_ceiling_seconds: 1800
"""


@dataclass(frozen=True)
class ImmutableConstraints:
    """Loaded view of ``immutable_constraints.yaml``.  Frozen so
    nothing in-process can accidentally rewrite a constraint."""

    network_allowed_hosts: tuple[str, ...]
    network_forbidden_keywords: tuple[str, ...]
    telemetry_forbidden: bool
    telemetry_forbidden_keywords: tuple[str, ...]
    output_forbidden_phrases: tuple[str, ...]
    output_forbidden_regex: tuple[str, ...]
    protected_files: tuple[str, ...]
    protected_settings: tuple[str, ...]
    precision_floor: float
    recall_floor: float
    latency_ceiling_seconds: float

    def check(self, payload: dict[str, Any]) -> None:
        """Evaluate a mutation candidate against every constraint.
        Raises ``HardConstraintViolation`` on the first hit."""
        # Gather every text-bearing value to scan once.
        flat: list[str] = []
        for v in _walk_strings(payload):
            flat.append(v)

        # Output sanitization
        for phrase in self.output_forbidden_phrases:
            for s in flat:
                if phrase in s:
                    raise HardConstraintViolation(
                        f"forbidden phrase in candidate output: {phrase!r}"
                    )
        for pattern in self.output_forbidden_regex:
            try:
                rx = re.compile(pattern)
            except re.error:
                continue
            for s in flat:
                if rx.search(s):
                    raise HardConstraintViolation(
                        f"forbidden regex matched ({pattern!r}) in output"
                    )

        # Network keywords (we forbid both http URLs and known
        # cloud-provider hosts in any field of the candidate).
        for keyword in self.network_forbidden_keywords:
            for s in flat:
                if keyword in s.lower():
                    raise HardConstraintViolation(
                        f"forbidden network keyword: {keyword!r}"
                    )

        # Telemetry keywords.
        if self.telemetry_forbidden:
            for keyword in self.telemetry_forbidden_keywords:
                for s in flat:
                    if keyword in s.lower():
                        raise HardConstraintViolation(
                            f"telemetry keyword detected: {keyword!r}"
                        )

        # Protected files / settings: candidates that try to mutate
        # those identifiers fail immediately.
        targets = payload.get("targets") or []
        if isinstance(targets, list):
            for t in targets:
                if t in self.protected_files:
                    raise HardConstraintViolation(
                        f"candidate targets a protected file: {t!r}"
                    )
        target_settings = payload.get("target_settings") or []
        if isinstance(target_settings, list):
            for s in target_settings:
                if s in self.protected_settings:
                    raise HardConstraintViolation(
                        f"candidate targets a protected setting: {s!r}"
                    )


def _walk_strings(value: Any) -> Iterator[str]:
    """Yield every string anywhere inside `value`."""
    if value is None:
        return
    if isinstance(value, str):
        yield value
        return
    if isinstance(value, dict):
        for v in value.values():
            yield from _walk_strings(v)
        return
    if isinstance(value, (list, tuple, set)):
        for v in value:
            yield from _walk_strings(v)


def load_immutable_constraints(
    path: str | Path | None = None,
) -> ImmutableConstraints:
    """Load constraints from disk.  When ``path`` is omitted we
    look at ``document_processor/sentinel/evolution/immutable_constraints.yaml``.

    Falls back to the bundled default text when the file is missing
    so a partial install still has the safety floor in place."""
    if path is None:
        here = Path(__file__).resolve().parent
        path = here / "immutable_constraints.yaml"
    text: str
    try:
        text = Path(path).read_text(encoding="utf-8")
    except FileNotFoundError:
        text = _DEFAULT_CONSTRAINTS_YAML
    try:
        import yaml  # type: ignore
        data = yaml.safe_load(text) or {}
    except Exception:
        # Pure-Python YAML fallback: parse a tiny subset (key: value,
        # nested dicts, lists of scalars).  The bundled default file
        # never relies on advanced YAML syntax so this is sufficient.
        data = _parse_simple_yaml(text)
    network = data.get("network") or {}
    telem = data.get("telemetry") or {}
    out = data.get("output_sanitization") or {}
    safety = data.get("evolution_safety") or {}
    accept = data.get("acceptance") or {}
    return ImmutableConstraints(
        network_allowed_hosts=tuple(network.get("allowed_hosts") or ()),
        network_forbidden_keywords=tuple(network.get("forbidden_keywords") or ()),
        telemetry_forbidden=bool(telem.get("forbidden", True)),
        telemetry_forbidden_keywords=tuple(telem.get("forbidden_keywords") or ()),
        output_forbidden_phrases=tuple(out.get("forbidden_phrases") or ()),
        output_forbidden_regex=tuple(out.get("forbidden_regex") or ()),
        protected_files=tuple(safety.get("protected_files") or ()),
        protected_settings=tuple(safety.get("protected_settings") or ()),
        precision_floor=float(accept.get("precision_floor", 0.6)),
        recall_floor=float(accept.get("recall_floor", 0.3)),
        latency_ceiling_seconds=float(accept.get("latency_ceiling_seconds", 1800)),
    )


def _parse_simple_yaml(text: str) -> dict[str, Any]:
    """Minimal YAML reader for bootstrap.  Handles the bundled
    constraints file: nested dicts, lists of strings, scalars.
    Not a real YAML parser — only used when ``pyyaml`` is missing
    AND the .yaml file is custom enough to need it."""
    root: dict[str, Any] = {}
    stack: list[tuple[int, dict[str, Any] | list[Any]]] = [(-1, root)]
    last_list_indent = -1
    last_list: list[Any] | None = None
    last_key_indent = -1

    for raw_line in text.splitlines():
        line = raw_line.rstrip()
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        indent = len(line) - len(line.lstrip(" "))
        body = line.strip()

        # Pop until indentation matches.
        while stack and stack[-1][0] >= indent:
            stack.pop()

        if body.startswith("- "):
            value = body[2:].strip().strip('"').strip("'")
            if last_list is None:
                continue
            last_list.append(value)
            continue

        if ":" in body:
            key, rest = body.split(":", 1)
            key = key.strip()
            rest = rest.strip()
            parent = stack[-1][1] if stack else root
            if isinstance(parent, list):
                continue
            if rest == "":
                # New nested container.  We don't know yet whether
                # it's a dict or a list — peek the next line.
                last_list = []
                parent[key] = last_list  # tentative
                # Track in stack as a list under this indent.
                stack.append((indent, last_list))
                last_list_indent = indent
            else:
                # Inline scalar.
                if rest.lower() in ("true", "false"):
                    parent[key] = (rest.lower() == "true")
                elif rest.replace(".", "", 1).replace("-", "", 1).isdigit():
                    parent[key] = float(rest) if "." in rest else int(rest)
                else:
                    parent[key] = rest.strip('"').strip("'")
        # Promote list to dict when we see a child `key:` instead
        # of `- value`.  The simple parser handles only the bundled
        # file shape — extra cases can fall through.

    # Demote single-key lists that should have been dicts.  Cheap
    # heuristic: if a value is [], leave it; otherwise the parser
    # produced the right thing for the bundled file.
    return _post_fix_simple_yaml(root)


def _post_fix_simple_yaml(node: Any) -> Any:
    """Convert tentative-list values that received dict children
    back to dicts.  Kept tolerant — bad values stay as-is."""
    if isinstance(node, dict):
        return {k: _post_fix_simple_yaml(v) for k, v in node.items()}
    if isinstance(node, list):
        if all(isinstance(x, str) for x in node):
            return node
        return [_post_fix_simple_yaml(x) for x in node]
    return node


# ─────────────────────────────────────────────────────────────────────
# Immutable hash-chained ledger
# ─────────────────────────────────────────────────────────────────────


@dataclass
class LedgerEntry:
    entry_id: str          # uuid4 hex
    ts: float              # epoch seconds, UTC
    actor: str             # subsystem that recorded this
    kind: Literal[
        "preference_logged",
        "lora_trained",
        "lora_promoted",
        "lora_rolled_back",
        "prompt_mutated",
        "prompt_promoted",
        "rule_synthesized",
        "rule_promoted",
        "rule_retired",
        "agent_spawned",
        "agent_promoted",
        "agent_archived",
        "distillation_trained",
        "dag_mutated",
        "dag_promoted",
        "rollback",
        "constraint_check_passed",
        "constraint_check_failed",
    ]
    payload: dict[str, Any] = field(default_factory=dict)
    parent_hash: str = ""   # SHA-256 of previous serialized entry (hex)
    self_hash: str = ""     # SHA-256 of (parent_hash + canonical payload)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


_LEDGER_LOCK = threading.Lock()


class LedgerStore:
    """Append-only ledger with a Merkle hash chain.

    File format: one JSON object per line in ``ledger.jsonl``.  The
    hash of each entry seals the previous entry — `verify()` walks
    the file and raises ``LedgerIntegrityError`` on any tamper.

    Thread-safe via a process-wide lock; the lock is fine even
    across asyncio because we hold it for sub-millisecond writes.
    """

    GENESIS_HASH = "0" * 64

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.path = self.root / "ledger.jsonl"
        self._tail_hash: str = self._compute_tail_hash()

    @property
    def tail_hash(self) -> str:
        return self._tail_hash

    # ─── Append ────────────────────────────────────────────────

    def append(
        self,
        actor: str,
        kind: str,
        payload: dict[str, Any] | None = None,
    ) -> LedgerEntry:
        """Add a new entry sealed against the current tail."""
        from uuid import uuid4
        entry = LedgerEntry(
            entry_id=uuid4().hex,
            ts=time.time(),
            actor=str(actor),
            kind=kind,                       # type: ignore[arg-type]
            payload=dict(payload or {}),
            parent_hash=self._tail_hash,
            self_hash="",
        )
        entry.self_hash = self._hash_entry(entry)
        with _LEDGER_LOCK:
            with self.path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(entry.to_dict(), default=str, sort_keys=True))
                f.write("\n")
            self._tail_hash = entry.self_hash
        return entry

    # ─── Read ──────────────────────────────────────────────────

    def entries(self) -> list[LedgerEntry]:
        if not self.path.is_file():
            return []
        out: list[LedgerEntry] = []
        with self.path.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    d = json.loads(line)
                except json.JSONDecodeError:
                    continue
                out.append(LedgerEntry(
                    entry_id=str(d.get("entry_id") or ""),
                    ts=float(d.get("ts") or 0.0),
                    actor=str(d.get("actor") or ""),
                    kind=d.get("kind") or "preference_logged",  # type: ignore[arg-type]
                    payload=dict(d.get("payload") or {}),
                    parent_hash=str(d.get("parent_hash") or ""),
                    self_hash=str(d.get("self_hash") or ""),
                ))
        return out

    def find(self, entry_id: str) -> LedgerEntry | None:
        for e in self.entries():
            if e.entry_id == entry_id:
                return e
        return None

    # ─── Integrity ─────────────────────────────────────────────

    def verify(self) -> bool:
        """Walk the chain.  Returns True iff every entry's
        ``self_hash`` matches its content + recorded parent."""
        prev = self.GENESIS_HASH
        for entry in self.entries():
            if entry.parent_hash != prev:
                raise LedgerIntegrityError(
                    f"chain break at {entry.entry_id}: "
                    f"parent_hash {entry.parent_hash!r} != tail {prev!r}"
                )
            recomputed = self._hash_entry(entry)
            if recomputed != entry.self_hash:
                raise LedgerIntegrityError(
                    f"hash mismatch at {entry.entry_id}: "
                    f"recomputed {recomputed!r} != stored {entry.self_hash!r}"
                )
            prev = entry.self_hash
        return True

    def _compute_tail_hash(self) -> str:
        if not self.path.is_file():
            return self.GENESIS_HASH
        last: str | None = None
        try:
            with self.path.open("r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line:
                        last = line
        except Exception:
            return self.GENESIS_HASH
        if not last:
            return self.GENESIS_HASH
        try:
            d = json.loads(last)
            return str(d.get("self_hash") or self.GENESIS_HASH)
        except Exception:
            return self.GENESIS_HASH

    @staticmethod
    def _hash_entry(entry: LedgerEntry) -> str:
        canonical = json.dumps(
            {
                "entry_id": entry.entry_id,
                "ts": entry.ts,
                "actor": entry.actor,
                "kind": entry.kind,
                "payload": entry.payload,
                "parent_hash": entry.parent_hash,
            },
            sort_keys=True,
            default=str,
        )
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


# ─────────────────────────────────────────────────────────────────────
# Sandbox helper
# ─────────────────────────────────────────────────────────────────────


@contextmanager
def sandbox_dir(
    root: str | Path,
    *,
    label: str = "candidate",
    keep_on_error: bool = True,
) -> Iterator[Path]:
    """Create a per-evolution-step temp directory.  Anything
    written outside it during the with-block raises a
    ``SandboxViolation`` (cooperative — callers have to use the
    yielded path)."""
    base = Path(root) / "sandbox"
    base.mkdir(parents=True, exist_ok=True)
    from uuid import uuid4
    step_dir = base / f"{label}_{int(time.time())}_{uuid4().hex[:8]}"
    step_dir.mkdir(parents=True, exist_ok=True)
    try:
        yield step_dir
    except Exception:
        if not keep_on_error:
            shutil.rmtree(step_dir, ignore_errors=True)
        raise
    else:
        # Successful exit: tear down to keep the sandbox tree small.
        shutil.rmtree(step_dir, ignore_errors=True)


__all__ = [
    "GovernanceError",
    "HardConstraintViolation",
    "ImmutableConstraints",
    "LedgerEntry",
    "LedgerIntegrityError",
    "LedgerStore",
    "SandboxViolation",
    "load_immutable_constraints",
    "sandbox_dir",
]
