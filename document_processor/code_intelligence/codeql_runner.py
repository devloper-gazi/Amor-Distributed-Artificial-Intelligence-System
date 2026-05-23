"""
Cycle G G3 — CodeQL hot-path integration.

Wraps GitHub's CodeQL CLI as an async subprocess runner that produces
`AnalysisIssue` rows for the existing
`StaticAnalysisHarness._run_*` gather block.  Findings show up
alongside pylint / mypy / bandit / radon on the code review surface
and feed `_score_candidate`'s static-analysis slot (engine.py:1334).

Why CodeQL on top of bandit
---------------------------
* Bandit catches single-statement patterns (eval, exec, hardcoded
  secret strings).  CodeQL does TAINT TRACKING across function +
  module boundaries — finds SQL injection, XSS, path traversal,
  insecure deserialisation that bandit misses.
* CodeQL queries are SQL-like and well-maintained by GitHub's
  Security Lab — much higher signal than bandit for OWASP top 10.

Trade-off
---------
* CodeQL adds ~30-60s per Python session (database create + run).
  Mitigated by:
  - Caching the CodeQL database keyed on `sha256(code)` so re-runs
    on identical code skip the rebuild
  - Setting `code_codeql_enabled=False` (default) — operator
    explicitly opts in once they've verified the CLI is installed
* CodeQL CLI is a ~600 MB bundle, NOT shipped with python:3.11-slim.
  Operator installs via `gh extension install github/gh-codeql`
  or downloads the bundle from
  https://github.com/github/codeql-cli-binaries/releases.  The
  runner's `_codeql_available()` check skips gracefully when the
  binary is missing — production stays green on hosts without it.

Result mapping
--------------
CodeQL SARIF 2.1.0 `runs[].results[]` rows map to AnalysisIssue:
  level=error     → severity="error"
  level=warning   → severity="warning"
  level=note/info → severity="info"
  security tags   → severity="security" (regardless of level)
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable, List, Optional

logger = logging.getLogger(__name__)


# Cache directory for CodeQL databases keyed on code-hash.  Reused
# across sessions so a debug-retry doesn't rebuild the same DB.
DEFAULT_DB_CACHE_ROOT = Path(
    os.environ.get(
        "AMOR_CODEQL_CACHE_ROOT",
        str(Path.home() / ".amor" / "codeql_db_cache"),
    )
)


@dataclass(frozen=True)
class CodeQLFinding:
    """One SARIF row, post-normalisation."""
    severity: str            # error | warning | info | security
    rule_id: str             # SARIF rule identifier, e.g. "py/sql-injection"
    message: str
    line: Optional[int]
    col: Optional[int]
    file: Optional[str]      # relative path inside the analysed snippet

    def to_dict(self) -> dict:
        return {
            "severity": self.severity,
            "rule_id": self.rule_id,
            "message": self.message,
            "line": self.line,
            "col": self.col,
            "file": self.file,
        }


# ─── SARIF → finding normalisation ─────────────────────────────────


_SEVERITY_MAP = {
    "error": "error",
    "warning": "warning",
    "warn": "warning",
    "info": "info",
    "note": "info",
    "none": "info",
}

# SARIF `properties.tags` that indicate security findings — they get
# bumped to severity="security" regardless of CodeQL's level field.
_SECURITY_TAGS = {
    "security", "external/cwe", "external/cve", "owasp",
    "injection", "path-injection", "command-injection",
    "sql-injection", "xss", "deserialization",
}


def _is_security_finding(rule: dict, rule_id_fallback: str = "") -> bool:
    """Inspect SARIF rule.properties.tags + rule.id for security
    indicators.  Used to escalate non-error findings into the
    "security" severity bucket.

    ``rule_id_fallback`` covers SARIF documents where the run's
    `rules[]` array is empty (each result inlines its ruleId but
    doesn't define the rule object) — we still want the keyword
    check to fire against the result's ruleId string."""
    props = rule.get("properties") or {}
    tags = props.get("tags") or []
    tags_set = {t.lower() for t in tags if isinstance(t, str)}
    if tags_set & _SECURITY_TAGS:
        return True
    if "security-severity" in props:
        return True
    rule_id = (rule.get("id") or rule_id_fallback or "").lower()
    if any(t in rule_id for t in ("sql", "xss", "injection", "deserial", "secret")):
        return True
    return False


def parse_sarif(sarif: dict) -> List[CodeQLFinding]:
    """Coerce a SARIF 2.1.0 document into a list of CodeQLFinding.
    Tolerant of partial/malformed SARIF — drops unparseable rows
    rather than crashing the static-analysis sweep."""
    findings: List[CodeQLFinding] = []
    for run in sarif.get("runs") or []:
        # Build rule_id → rule object lookup so we can read properties.
        tool = run.get("tool", {}).get("driver", {})
        rules = {r.get("id"): r for r in (tool.get("rules") or []) if r.get("id")}
        for result in run.get("results") or []:
            rule_id = result.get("ruleId") or result.get("rule", {}).get("id") or ""
            rule = rules.get(rule_id, {})
            level = (result.get("level") or rule.get("defaultConfiguration", {}).get("level") or "warning").lower()
            severity = _SEVERITY_MAP.get(level, "warning")
            if _is_security_finding(rule, rule_id_fallback=rule_id):
                severity = "security"
            message_obj = result.get("message") or {}
            text = (
                message_obj.get("text")
                if isinstance(message_obj, dict)
                else str(message_obj)
            ) or ""
            # locations[0].physicalLocation.region.startLine
            locations = result.get("locations") or []
            line = col = None
            file_uri = None
            if locations:
                phys = locations[0].get("physicalLocation") or {}
                artifact = phys.get("artifactLocation") or {}
                file_uri = artifact.get("uri")
                region = phys.get("region") or {}
                line = region.get("startLine")
                col = region.get("startColumn")
            findings.append(CodeQLFinding(
                severity=severity,
                rule_id=rule_id or "unknown",
                message=text[:1000],
                line=line,
                col=col,
                file=file_uri,
            ))
    return findings


# ─── CLI availability + cache ──────────────────────────────────────


def _codeql_available() -> bool:
    """Cheap PATH check — returns False on hosts without the CLI
    so the harness skips CodeQL silently."""
    return shutil.which("codeql") is not None


def _code_hash(code: str) -> str:
    return hashlib.sha256(code.encode("utf-8", errors="replace")).hexdigest()


# ─── Runner ────────────────────────────────────────────────────────


async def run_codeql_python(
    code: str,
    *,
    timeout_s: float = 90.0,
    db_cache_root: Optional[Path] = None,
    queries: str = "python-security-and-quality.qls",
    binary: str = "codeql",
) -> List[CodeQLFinding]:
    """Run CodeQL against a Python code snippet.

    Skips silently (returns empty list) when:
      * the binary isn't on PATH
      * the subprocess exits non-zero (logs warning at debug level)
      * timeout fires

    Successful runs hit the per-code-hash cache so identical-code
    re-runs (debug-retry loop, reflexion) reuse the same database.
    """
    if not _codeql_available():
        logger.debug("codeql binary not on PATH — skipping")
        return []

    if not code:
        return []

    cache_root = db_cache_root or DEFAULT_DB_CACHE_ROOT
    cache_root.mkdir(parents=True, exist_ok=True)
    code_h = _code_hash(code)
    db_path = cache_root / f"py_{code_h[:16]}"
    sarif_path = cache_root / f"py_{code_h[:16]}.sarif"

    # Stage the snippet on disk for CodeQL to ingest.
    with tempfile.TemporaryDirectory() as src_dir:
        src = Path(src_dir) / "main.py"
        src.write_text(code, encoding="utf-8")

        # Database create (cache miss only).
        if not (db_path / "codeql-database.yml").is_file():
            db_path.mkdir(parents=True, exist_ok=True)
            cmd_create = [
                binary, "database", "create", str(db_path),
                "--language", "python",
                "--source-root", str(src_dir),
                "--overwrite", "--quiet",
            ]
            try:
                proc = await asyncio.create_subprocess_exec(
                    *cmd_create,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                _, stderr = await asyncio.wait_for(
                    proc.communicate(), timeout=timeout_s,
                )
                if proc.returncode != 0:
                    logger.warning(
                        "codeql database create failed rc=%s err=%s",
                        proc.returncode,
                        (stderr or b"").decode("utf-8", errors="replace")[:240],
                    )
                    return []
            except (asyncio.TimeoutError, FileNotFoundError) as exc:
                logger.warning("codeql database create exception: %s", exc)
                return []

        # Analyze.
        cmd_analyze = [
            binary, "database", "analyze", str(db_path),
            queries,
            "--format=sarif-latest",
            "--output", str(sarif_path),
            "--quiet", "--ram=2048",
        ]
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd_analyze,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            _, stderr = await asyncio.wait_for(
                proc.communicate(), timeout=timeout_s,
            )
            if proc.returncode != 0:
                logger.warning(
                    "codeql analyze failed rc=%s err=%s",
                    proc.returncode,
                    (stderr or b"").decode("utf-8", errors="replace")[:240],
                )
                return []
        except (asyncio.TimeoutError, FileNotFoundError) as exc:
            logger.warning("codeql analyze exception: %s", exc)
            return []

    # Parse SARIF.
    if not sarif_path.is_file():
        return []
    try:
        sarif = json.loads(sarif_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("codeql SARIF unparseable: %s", exc)
        return []

    return parse_sarif(sarif)


# ─── Convenience: convert to AnalysisIssue shape ───────────────────


def findings_to_analysis_issues(findings: List[CodeQLFinding]) -> List[dict]:
    """Project CodeQLFinding into the dict shape consumed by
    `StaticAnalysisResult.issues.append(AnalysisIssue(**d))`.

    Kept as a dict-returning helper so the caller in
    `static_analysis.py:_run_codeql` doesn't have to import this
    module's dataclass — keeps the dep graph one-way.
    """
    return [
        {
            "severity": f.severity,
            "code": f.rule_id,
            "message": f.message,
            "line": f.line,
            "col": f.col,
            "source": "codeql",
        }
        for f in findings
    ]
