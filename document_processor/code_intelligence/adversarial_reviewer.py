"""
AdversarialReviewer — synchronous event filter for prompt injection,
secret leakage, suspicious shell forms, and untrusted-URL execution
patterns.

The reviewer is wired into the engine's ``_publish`` path so EVERY
SSE event (in particular ``code_ready``, ``test_ready``,
``execution_result``, ``deliverable_ready``) is inspected before it
fans out. A match:

  1. Emits an ``adversarial_alert`` event of its own.
  2. Sets ``cancel_requested = True`` on the running session so the
     engine halts at the next phase boundary.
  3. Writes a record to MongoDB collection ``adversarial_events`` for
     later human review.

The rule pack lives in ``security/adversary_rules.yaml`` and is
hot-reloadable: call ``AdversarialReviewer.reload_rules()`` after
editing the YAML to pick up changes without an app restart.

Failure-quiet: a malformed YAML or unreachable Mongo MUST NOT poison
the event pipeline. The reviewer logs and lets the original event
through if it cannot run its checks.
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


_DEFAULT_RULES_PATH = Path(__file__).parent / "security" / "adversary_rules.yaml"


# ─────────────────────────────────────────────────────────────────────────────
# Compiled rule
# ─────────────────────────────────────────────────────────────────────────────


@dataclass
class CompiledRule:
    id: str
    severity: str  # "critical" | "high" | "medium" | "low"
    description: str
    pattern: re.Pattern
    targets: list[str] = field(default_factory=list)


# ─────────────────────────────────────────────────────────────────────────────
# Reviewer
# ─────────────────────────────────────────────────────────────────────────────


class AdversarialReviewer:
    """
    Inspects every event payload before it is published. On match,
    emits an alert, marks the session for cancellation, and persists
    a record.

    Use ``inspect_event(session_id, event)`` from inside the engine's
    ``_publish``. The method returns a tuple ``(allow, alert_event)``:
      - allow=True, alert=None   → publish original event unchanged.
      - allow=True, alert={...}  → publish original AND the alert.
      - allow=False, alert={...} → suppress original, publish alert.

    By default the reviewer is non-blocking: matches surface as
    parallel ``adversarial_alert`` events while the original still
    flows. Set ``block_on_critical=True`` to suppress critical-severity
    events (e.g. an exposed AWS key in ``code_ready``).
    """

    def __init__(
        self,
        rules_path: Path | None = None,
        block_on_critical: bool = True,
    ):
        self._rules_path = Path(rules_path) if rules_path else _DEFAULT_RULES_PATH
        self._block_on_critical = block_on_critical
        self._rules: list[CompiledRule] = []
        self._load_rules()

    # ── Rules ─────────────────────────────────────────────────────────────

    def _load_rules(self) -> None:
        try:
            import yaml  # type: ignore[import-not-found]
        except ImportError:  # pragma: no cover
            logger.warning("adversarial_reviewer_yaml_not_available rules_skipped")
            self._rules = []
            return

        try:
            doc = yaml.safe_load(self._rules_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            logger.warning(
                "adversarial_reviewer_rules_missing path=%s",
                self._rules_path,
            )
            self._rules = []
            return
        except Exception as exc:  # pragma: no cover
            logger.warning("adversarial_reviewer_yaml_parse_failed: %s", exc)
            self._rules = []
            return

        compiled: list[CompiledRule] = []
        for item in (doc or {}).get("rules") or []:
            if not isinstance(item, dict):
                continue
            rid = str(item.get("id") or "")
            if not rid:
                continue
            sev = str(item.get("severity") or "medium").lower()
            if sev not in {"critical", "high", "medium", "low"}:
                sev = "medium"
            pat_str = str(item.get("pattern") or "")
            if not pat_str:
                continue
            flags = re.MULTILINE
            if not item.get("case_sensitive", False):
                flags |= re.IGNORECASE
            try:
                pat = re.compile(pat_str, flags)
            except re.error as exc:
                logger.warning(
                    "adversarial_rule_regex_invalid id=%s err=%s",
                    rid,
                    exc,
                )
                continue
            targets = [str(t) for t in (item.get("targets") or [])]
            compiled.append(
                CompiledRule(
                    id=rid,
                    severity=sev,
                    description=str(item.get("description") or ""),
                    pattern=pat,
                    targets=targets,
                )
            )
        self._rules = compiled
        logger.info(
            "adversarial_reviewer_loaded rules=%d path=%s",
            len(self._rules),
            self._rules_path,
        )

    def reload_rules(self) -> int:
        """Re-read the YAML rule pack. Returns the new rule count."""
        self._load_rules()
        return len(self._rules)

    @property
    def rule_count(self) -> int:
        return len(self._rules)

    # ── Inspection ────────────────────────────────────────────────────────

    def inspect_event(
        self,
        session_id: str,
        event: dict[str, Any],
    ) -> tuple[bool, dict[str, Any] | None]:
        """
        Run every rule against the event payload.

        Returns ``(allow, alert)``. The caller (engine ``_publish``)
        publishes the alert (if any) AFTER the original event when
        allow=True, or INSTEAD of the original when allow=False.
        """
        if not self._rules:
            return True, None
        if not isinstance(event, dict):
            return True, None

        etype = str(event.get("type") or "")
        haystack = self._stringify(event)
        if not haystack:
            return True, None

        hits: list[dict[str, Any]] = []
        for rule in self._rules:
            if rule.targets and etype and etype not in rule.targets:
                continue
            m = rule.pattern.search(haystack)
            if m:
                hits.append(
                    {
                        "rule_id": rule.id,
                        "severity": rule.severity,
                        "description": rule.description,
                        "match_excerpt": haystack[max(0, m.start() - 40) : m.end() + 40][:200],
                    }
                )

        if not hits:
            return True, None

        worst = self._worst_severity(hits)
        alert = {
            "type": "adversarial_alert",
            "session_id": session_id,
            "source_event_type": etype,
            "severity": worst,
            "hits": hits,
            "detected_at": datetime.now(UTC).isoformat(),
        }

        # Persist to MongoDB best-effort. We only schedule the task when
        # an event loop is actually running — outside one (e.g. unit
        # tests calling inspect_event synchronously) `get_running_loop`
        # raises RuntimeError; we just skip persistence in that case.
        try:
            import asyncio as _asyncio

            try:
                _asyncio.get_running_loop()
            except RuntimeError:
                pass  # no loop → skip persistence (ok in sync contexts)
            else:
                _asyncio.create_task(self._persist_alert(session_id, alert))
        except Exception:  # pragma: no cover
            pass

        allow = not (self._block_on_critical and worst == "critical")
        return allow, alert

    @staticmethod
    def _worst_severity(hits: list[dict[str, Any]]) -> str:
        rank = {"critical": 4, "high": 3, "medium": 2, "low": 1}
        return max(
            (h.get("severity", "medium") for h in hits),
            key=lambda s: rank.get(s, 0),
        )

    @staticmethod
    def _stringify(event: dict[str, Any]) -> str:
        """
        Flatten an event into a single string for regex scanning. We
        skip event_id, timestamps, and other UUID-like fields to keep
        the haystack focused on substantive content.
        """
        skip = {"event_id", "session_id", "started_at", "completed_at"}
        parts: list[str] = []

        def _walk(node: Any) -> None:
            if isinstance(node, str):
                parts.append(node)
            elif isinstance(node, dict):
                for k, v in node.items():
                    if k in skip:
                        continue
                    _walk(v)
            elif isinstance(node, (list, tuple)):
                for v in node:
                    _walk(v)
            # numbers / bools / None ignored

        _walk(event)
        return "\n".join(parts)

    # ── Persistence ───────────────────────────────────────────────────────

    async def _persist_alert(
        self,
        session_id: str,
        alert: dict[str, Any],
    ) -> None:
        try:
            from ..infrastructure.storage import storage_manager

            db = storage_manager.mongo_db
            if db is None:
                return
            await db["adversarial_events"].insert_one(
                {
                    "session_id": session_id,
                    **alert,
                    "_at": time.time(),
                }
            )
        except Exception as exc:
            logger.debug("adversarial_persist_failed: %s", exc)
