"""
Sentinel — static analysis swarm.

Wraps the static-analysis CLI tools available on `$PATH`:

* **Always available** (Python deps in requirements.txt): bandit
  (security), pylint (style + bugs), mypy (type errors), radon
  (complexity).
* **Optional** (graceful skip when binary missing): semgrep,
  gitleaks, trivy, gosec, cppcheck, eslint with `eslint-plugin-
  security`, brakeman, phpstan, cargo-audit, pip-audit, npm-audit.

Every wrapper returns `list[Finding]`.  When a tool's binary is
missing the wrapper returns an empty list and emits a
``tool_skipped`` event so the engine + UI can show "X tools
skipped" without aborting.

Tool output is parsed into a stable Finding shape.  Severities are
coerced; CWE / OWASP IDs are extracted when the tool emits them;
unknown formats are tolerated rather than crashing.

License: MIT.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable

from .models import Finding, SeverityLevel, coerce_severity

logger = logging.getLogger(__name__)


EventCallback = Callable[[str, dict[str, Any]], Awaitable[None]] | None


# ─────────────────────────────────────────────────────────────────────
# Tool detection
# ─────────────────────────────────────────────────────────────────────


@dataclass
class ToolSpec:
    name: str
    binary: str | None = None       # cli name on PATH
    python_module: str | None = None  # e.g. "bandit" if shipped as a Py module
    languages: tuple[str, ...] = ()    # which languages it audits
    description: str = ""


# Tool registry — declarative.  Only tools with a binary on PATH or
# python_module importable will run.  Everything else returns empty.
DEFAULT_TOOLS: tuple[ToolSpec, ...] = (
    ToolSpec(
        name="bandit",
        python_module="bandit",
        languages=("python",),
        description="Security-focused linter for Python (CWE-aware).",
    ),
    ToolSpec(
        name="pylint",
        python_module="pylint",
        languages=("python",),
        description="Style + correctness linter for Python.",
    ),
    ToolSpec(
        name="mypy",
        python_module="mypy",
        languages=("python",),
        description="Static type checker for Python.",
    ),
    ToolSpec(
        name="radon",
        python_module="radon",
        languages=("python",),
        description="Cyclomatic complexity + maintainability metrics.",
    ),
    ToolSpec(
        name="semgrep",
        binary="semgrep",
        languages=("python", "javascript", "typescript", "go", "java"),
        description="Multi-language SAST (security-audit + secrets + owasp-top-ten rulesets).",
    ),
    ToolSpec(
        name="gitleaks",
        binary="gitleaks",
        languages=("*",),
        description="Secret scanner across the entire tree.",
    ),
    ToolSpec(
        name="trivy",
        binary="trivy",
        languages=("*",),
        description="Filesystem CVE / dependency vulnerability scanner.",
    ),
    ToolSpec(
        name="gosec",
        binary="gosec",
        languages=("go",),
        description="Security checks for Go source.",
    ),
    ToolSpec(
        name="cppcheck",
        binary="cppcheck",
        languages=("c", "cpp", "c++"),
        description="C/C++ correctness + buffer-overflow checker.",
    ),
)


def detect_tool(spec: ToolSpec) -> bool:
    """Return True when the tool can run on this host."""
    if spec.binary and shutil.which(spec.binary):
        return True
    if spec.python_module:
        try:  # noqa: SIM105 — explicit try preserves clarity
            __import__(spec.python_module)
            return True
        except Exception:
            return False
    return False


# ─────────────────────────────────────────────────────────────────────
# Subprocess helper
# ─────────────────────────────────────────────────────────────────────


async def _run_subprocess(
    cmd: list[str],
    *,
    cwd: str | None = None,
    timeout_s: float = 60.0,
    stdin_data: str | None = None,
) -> tuple[int, str, str]:
    """Run `cmd` and return ``(returncode, stdout, stderr)``.  Hard-
    kills on timeout; returns (124, "", "timeout") on TimeoutError so
    callers can degrade gracefully."""
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            cwd=cwd,
            stdin=asyncio.subprocess.PIPE if stdin_data else None,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except FileNotFoundError:
        return (127, "", f"binary not found: {cmd[0]}")
    except Exception as exc:  # pragma: no cover - infra
        return (1, "", f"{type(exc).__name__}: {exc}")
    try:
        stdout_b, stderr_b = await asyncio.wait_for(
            proc.communicate(stdin_data.encode("utf-8") if stdin_data else None),
            timeout=timeout_s,
        )
    except asyncio.TimeoutError:
        try:
            proc.kill()
        except ProcessLookupError:
            pass
        await proc.wait()
        return (124, "", f"timeout after {timeout_s}s")
    return (
        int(proc.returncode or 0),
        stdout_b.decode("utf-8", errors="replace"),
        stderr_b.decode("utf-8", errors="replace"),
    )


# ─────────────────────────────────────────────────────────────────────
# Per-tool parsers
# ─────────────────────────────────────────────────────────────────────


_BANDIT_SEVERITY_MAP: dict[str, SeverityLevel] = {
    "LOW": "low", "MEDIUM": "medium", "HIGH": "high",
}


def parse_bandit_json(payload: str) -> list[Finding]:
    """Bandit JSON: ``{"results": [{filename, line_number, test_id,
    issue_severity, issue_text, cwe?: {id}}]``."""
    findings: list[Finding] = []
    try:
        data = json.loads(payload or "{}")
    except json.JSONDecodeError:
        return findings
    for r in data.get("results") or []:
        cwe = ""
        cwe_obj = r.get("cwe")
        if isinstance(cwe_obj, dict):
            cwe = f"CWE-{cwe_obj.get('id')}" if cwe_obj.get("id") else ""
        elif isinstance(cwe_obj, (str, int)):
            cwe = f"CWE-{cwe_obj}"
        findings.append(
            Finding(
                tool="bandit",
                source_kind="static_tool",
                rule_id=str(r.get("test_id") or r.get("test_name") or ""),
                file=str(r.get("filename") or ""),
                line_start=int(r.get("line_number") or 0),
                line_end=int(r.get("line_number") or 0),
                raw_message=str(r.get("issue_text") or "")[:2000],
                code_snippet=str(r.get("code") or "")[:2000],
                language="python",
                severity=_BANDIT_SEVERITY_MAP.get(
                    str(r.get("issue_severity") or "").upper(), "low",
                ),
                confidence=float({"LOW": 0.4, "MEDIUM": 0.6, "HIGH": 0.85}.get(
                    str(r.get("issue_confidence") or "").upper(), 0.5,
                )),
                cwe=cwe,
                source_weight=0.65,
            )
        )
    return findings


def parse_semgrep_json(payload: str) -> list[Finding]:
    """Semgrep JSON: ``{"results": [{check_id, path, start.line,
    extra.severity, extra.message, extra.metadata.cwe}]``."""
    findings: list[Finding] = []
    try:
        data = json.loads(payload or "{}")
    except json.JSONDecodeError:
        return findings
    for r in data.get("results") or []:
        extra = r.get("extra") or {}
        meta = extra.get("metadata") or {}
        # CWE may be a list ["CWE-89: ..."] or a string.
        cwe_raw = meta.get("cwe") or meta.get("CWE") or ""
        if isinstance(cwe_raw, list):
            cwe_raw = cwe_raw[0] if cwe_raw else ""
        cwe = ""
        m = re.search(r"CWE-(\d+)", str(cwe_raw))
        if m:
            cwe = f"CWE-{m.group(1)}"
        owasp = ""
        owasp_raw = meta.get("owasp") or ""
        if isinstance(owasp_raw, list):
            owasp_raw = owasp_raw[0] if owasp_raw else ""
        owasp = str(owasp_raw or "")[:40]
        start = r.get("start") or {}
        end = r.get("end") or {}
        findings.append(
            Finding(
                tool="semgrep",
                source_kind="static_tool",
                rule_id=str(r.get("check_id") or ""),
                file=str(r.get("path") or ""),
                line_start=int(start.get("line") or 0),
                line_end=int(end.get("line") or start.get("line") or 0),
                column_start=int(start.get("col") or 0),
                column_end=int(end.get("col") or 0),
                raw_message=str(extra.get("message") or "")[:2000],
                code_snippet=str(extra.get("lines") or "")[:2000],
                language=str(extra.get("metavars", {}).get("language", "")),
                severity=coerce_severity(extra.get("severity"), default="medium"),
                confidence=float(
                    {"INFO": 0.3, "WARNING": 0.6, "ERROR": 0.85}.get(
                        str(extra.get("severity") or "").upper(), 0.6,
                    )
                ),
                cwe=cwe,
                owasp=owasp,
                source_weight=0.7,
            )
        )
    return findings


def parse_gitleaks_json(payload: str) -> list[Finding]:
    """Gitleaks JSON: top-level array of ``{Description, File,
    StartLine, EndLine, RuleID, Match, Secret}``."""
    findings: list[Finding] = []
    try:
        data = json.loads(payload or "[]")
    except json.JSONDecodeError:
        return findings
    if not isinstance(data, list):
        return findings
    for r in data:
        findings.append(
            Finding(
                tool="gitleaks",
                source_kind="static_tool",
                rule_id=str(r.get("RuleID") or "secret"),
                file=str(r.get("File") or ""),
                line_start=int(r.get("StartLine") or 0),
                line_end=int(r.get("EndLine") or r.get("StartLine") or 0),
                raw_message=str(r.get("Description") or "Hardcoded secret detected")[:2000],
                # Don't dump the actual secret into the report — Gitleaks
                # already redacts via --redact, but keep a short
                # fingerprint of what was caught.
                code_snippet=("<redacted secret>"),
                severity="critical",   # leaked secrets are always critical
                confidence=0.9,
                cwe="CWE-798",         # Use of Hard-coded Credentials
                source_weight=0.85,
            )
        )
    return findings


def parse_trivy_json(payload: str) -> list[Finding]:
    """Trivy filesystem JSON: ``{"Results": [{"Target", "Vulnerabilities":
    [{VulnerabilityID, PkgName, InstalledVersion, Severity,
    Title, Description, References, CweIDs}]}]``."""
    findings: list[Finding] = []
    try:
        data = json.loads(payload or "{}")
    except json.JSONDecodeError:
        return findings
    for result in data.get("Results") or []:
        target = str(result.get("Target") or "")
        for v in result.get("Vulnerabilities") or []:
            cwe_ids = v.get("CweIDs") or []
            cwe = cwe_ids[0] if cwe_ids else ""
            findings.append(
                Finding(
                    tool="trivy",
                    source_kind="static_tool",
                    rule_id=str(v.get("VulnerabilityID") or ""),
                    file=target,
                    raw_message=str(
                        v.get("Title") or v.get("Description") or ""
                    )[:2000],
                    severity=coerce_severity(v.get("Severity"), default="medium"),
                    confidence=0.8,
                    cwe=str(cwe or ""),
                    cvss_base_score=float(
                        ((v.get("CVSS") or {}).get("nvd") or {}).get(
                            "V3Score", 0.0
                        ) or 0.0
                    ),
                    extra={
                        "package": v.get("PkgName"),
                        "installed": v.get("InstalledVersion"),
                        "fixed": v.get("FixedVersion"),
                    },
                    source_weight=0.75,
                )
            )
    return findings


def parse_pylint_json(payload: str) -> list[Finding]:
    """Pylint JSON: list of ``{type, module, path, line, column,
    message, message-id, symbol}``."""
    findings: list[Finding] = []
    try:
        data = json.loads(payload or "[]")
    except json.JSONDecodeError:
        return findings
    if not isinstance(data, list):
        return findings
    for r in data:
        sev_map = {
            "fatal": "critical", "error": "high",
            "warning": "medium", "convention": "low",
            "refactor": "low", "info": "info",
        }
        findings.append(
            Finding(
                tool="pylint",
                source_kind="static_tool",
                rule_id=str(r.get("message-id") or r.get("symbol") or ""),
                file=str(r.get("path") or ""),
                line_start=int(r.get("line") or 0),
                column_start=int(r.get("column") or 0),
                raw_message=str(r.get("message") or "")[:2000],
                language="python",
                severity=sev_map.get(str(r.get("type") or "").lower(), "low"),  # type: ignore[arg-type]
                confidence=0.5,
                source_weight=0.45,  # pylint signal is noisy → weighted lower
            )
        )
    return findings


def parse_mypy_json(payload: str) -> list[Finding]:
    """Mypy --output=json: one JSON object per line."""
    findings: list[Finding] = []
    for raw_line in (payload or "").splitlines():
        line = raw_line.strip()
        if not line or not line.startswith("{"):
            continue
        try:
            r = json.loads(line)
        except json.JSONDecodeError:
            continue
        sev_map = {"error": "high", "note": "info", "warning": "medium"}
        findings.append(
            Finding(
                tool="mypy",
                source_kind="static_tool",
                rule_id=str(r.get("code") or ""),
                file=str(r.get("file") or r.get("path") or ""),
                line_start=int(r.get("line") or 0),
                column_start=int(r.get("column") or 0),
                raw_message=str(r.get("message") or "")[:2000],
                language="python",
                severity=sev_map.get(str(r.get("severity") or "").lower(), "low"),  # type: ignore[arg-type]
                confidence=0.55,
                source_weight=0.45,
            )
        )
    return findings


def parse_gosec_json(payload: str) -> list[Finding]:
    """Gosec JSON: ``{"Issues": [{file, line, severity, confidence,
    rule_id, details, cwe: {ID}}]}``."""
    findings: list[Finding] = []
    try:
        data = json.loads(payload or "{}")
    except json.JSONDecodeError:
        return findings
    for r in data.get("Issues") or []:
        cwe_obj = r.get("cwe") or {}
        cwe = f"CWE-{cwe_obj.get('ID')}" if cwe_obj.get("ID") else ""
        findings.append(
            Finding(
                tool="gosec",
                source_kind="static_tool",
                rule_id=str(r.get("rule_id") or ""),
                file=str(r.get("file") or ""),
                line_start=int(str(r.get("line") or "0").split("-")[0]),
                raw_message=str(r.get("details") or "")[:2000],
                language="go",
                severity=coerce_severity(r.get("severity"), default="medium"),
                confidence=float({"LOW": 0.4, "MEDIUM": 0.6, "HIGH": 0.85}.get(
                    str(r.get("confidence") or "").upper(), 0.5,
                )),
                cwe=cwe,
                source_weight=0.7,
            )
        )
    return findings


def parse_cppcheck_xml(payload: str) -> list[Finding]:
    """Cppcheck XML2 (best-effort regex parse — keeps lxml optional).

    The output looks like::

        <error id="bufferOverrun" severity="error" msg="Buffer overrun" cwe="120">
            <location file="src/main.c" line="42"/>
        </error>

    We extract the ``<error ...>`` open tag plus the inner body (until
    ``</error>``), then pull the cwe attribute via a dedicated match
    so attribute order doesn't matter.  ``<location>`` is parsed for
    file + line out of the body.
    """
    findings: list[Finding] = []
    if not payload:
        return findings

    error_pat = re.compile(r"<error\b([^>]*)>(.*?)</error>", re.S)
    attr_pat = re.compile(r'(\w+)="([^"]*)"')
    loc_pat = re.compile(
        r'<location\b[^/>]*file="([^"]+)"[^/>]*line="(\d+)"'
    )
    for m in error_pat.finditer(payload):
        attrs = dict(attr_pat.findall(m.group(1) or ""))
        body = m.group(2) or ""
        loc = loc_pat.search(body)
        file_, line = "", 0
        if loc:
            file_ = loc.group(1)
            try:
                line = int(loc.group(2))
            except ValueError:
                line = 0
        cwe_num = attrs.get("cwe", "")
        findings.append(
            Finding(
                tool="cppcheck",
                source_kind="static_tool",
                rule_id=attrs.get("id", ""),
                file=file_,
                line_start=line,
                raw_message=attrs.get("msg", "")[:2000],
                language="cpp",
                severity=coerce_severity(attrs.get("severity"), default="medium"),
                confidence=0.6,
                cwe=f"CWE-{cwe_num}" if cwe_num else "",
                source_weight=0.7,
            )
        )
    return findings


# ─────────────────────────────────────────────────────────────────────
# Per-tool runners (subprocess invocation)
# ─────────────────────────────────────────────────────────────────────


async def run_bandit(paths: list[str], *, timeout_s: float = 60.0) -> list[Finding]:
    if not paths:
        return []
    cmd = [sys.executable, "-m", "bandit", "-f", "json", "-q", "-r", *paths]
    rc, stdout, stderr = await _run_subprocess(cmd, timeout_s=timeout_s)
    # Bandit returns 1 when issues are found — that's fine, we still
    # parse stdout.  127 = binary not found.  124 = timeout.
    if rc in (124, 127):
        logger.debug("bandit skipped: rc=%s err=%s", rc, stderr[:120])
        return []
    return parse_bandit_json(stdout)


async def run_pylint(paths: list[str], *, timeout_s: float = 90.0) -> list[Finding]:
    if not paths:
        return []
    cmd = [sys.executable, "-m", "pylint", "--output-format=json", *paths]
    rc, stdout, _stderr = await _run_subprocess(cmd, timeout_s=timeout_s)
    if rc in (124, 127):
        return []
    return parse_pylint_json(stdout)


async def run_mypy(paths: list[str], *, timeout_s: float = 90.0) -> list[Finding]:
    if not paths:
        return []
    cmd = [
        sys.executable, "-m", "mypy",
        "--output=json", "--ignore-missing-imports", "--no-color-output",
        *paths,
    ]
    rc, stdout, _stderr = await _run_subprocess(cmd, timeout_s=timeout_s)
    if rc in (124, 127):
        return []
    return parse_mypy_json(stdout)


async def run_semgrep(paths: list[str], *, timeout_s: float = 180.0) -> list[Finding]:
    if not paths:
        return []
    if not shutil.which("semgrep"):
        return []
    cmd = [
        "semgrep",
        "--config", "p/security-audit",
        "--config", "p/secrets",
        "--config", "p/owasp-top-ten",
        "--json", "--quiet", "--metrics=off", "--no-rewrite-rule-ids",
        *paths,
    ]
    rc, stdout, _stderr = await _run_subprocess(cmd, timeout_s=timeout_s)
    if rc in (124, 127):
        return []
    return parse_semgrep_json(stdout)


async def run_gitleaks(paths: list[str], *, timeout_s: float = 90.0) -> list[Finding]:
    if not paths:
        return []
    if not shutil.which("gitleaks"):
        return []
    findings: list[Finding] = []
    for p in paths:
        cmd = [
            "gitleaks", "detect",
            "--no-banner", "--redact", "--report-format", "json",
            "--report-path", "-",
            "--source", p,
        ]
        rc, stdout, _stderr = await _run_subprocess(cmd, timeout_s=timeout_s)
        # Gitleaks rc=1 → leaks found (still parse stdout)
        if rc in (124, 127):
            continue
        findings.extend(parse_gitleaks_json(stdout))
    return findings


async def run_trivy(paths: list[str], *, timeout_s: float = 240.0) -> list[Finding]:
    if not paths:
        return []
    if not shutil.which("trivy"):
        return []
    findings: list[Finding] = []
    for p in paths:
        cmd = [
            "trivy", "fs", "--offline-scan",
            "--format", "json", "--quiet",
            "--severity", "LOW,MEDIUM,HIGH,CRITICAL",
            p,
        ]
        rc, stdout, _stderr = await _run_subprocess(cmd, timeout_s=timeout_s)
        if rc in (124, 127):
            continue
        findings.extend(parse_trivy_json(stdout))
    return findings


async def run_gosec(paths: list[str], *, timeout_s: float = 90.0) -> list[Finding]:
    if not paths:
        return []
    if not shutil.which("gosec"):
        return []
    findings: list[Finding] = []
    for p in paths:
        cmd = ["gosec", "-fmt", "json", "-no-fail", "./..."]
        rc, stdout, _stderr = await _run_subprocess(
            cmd, timeout_s=timeout_s, cwd=p,
        )
        if rc in (124, 127):
            continue
        findings.extend(parse_gosec_json(stdout))
    return findings


async def run_cppcheck(paths: list[str], *, timeout_s: float = 120.0) -> list[Finding]:
    if not paths:
        return []
    if not shutil.which("cppcheck"):
        return []
    cmd = [
        "cppcheck",
        "--enable=all", "--inconclusive",
        "--xml", "--xml-version=2",
        *paths,
    ]
    rc, _stdout, stderr = await _run_subprocess(cmd, timeout_s=timeout_s)
    # cppcheck emits XML on STDERR by convention.
    if rc in (124, 127):
        return []
    return parse_cppcheck_xml(stderr)


# ─────────────────────────────────────────────────────────────────────
# StaticSwarm orchestrator
# ─────────────────────────────────────────────────────────────────────


# Map tool name → runner (so engine can pick a subset by scan profile).
_RUNNERS: dict[str, Callable[..., Awaitable[list[Finding]]]] = {
    "bandit":   run_bandit,
    "pylint":   run_pylint,
    "mypy":     run_mypy,
    "semgrep":  run_semgrep,
    "gitleaks": run_gitleaks,
    "trivy":    run_trivy,
    "gosec":    run_gosec,
    "cppcheck": run_cppcheck,
}


@dataclass
class StaticSwarmResult:
    findings: list[Finding]
    tools_run: list[str]
    tools_skipped: list[str]
    elapsed_ms: float


class StaticSwarm:
    """Runs every available static-analysis tool in parallel."""

    DEFAULT_QUICK_TOOLS = ("bandit", "gitleaks")
    DEFAULT_STANDARD_TOOLS = ("bandit", "pylint", "gitleaks", "semgrep")
    DEFAULT_DEEP_TOOLS = (
        "bandit", "pylint", "mypy", "semgrep",
        "gitleaks", "trivy", "gosec", "cppcheck",
    )

    def __init__(
        self,
        *,
        tools: tuple[str, ...] | None = None,
        timeout_per_tool_s: float = 120.0,
        on_event: EventCallback = None,
    ) -> None:
        if tools is None:
            tools = tuple(_RUNNERS.keys())
        # Skip unknown tool names rather than crashing.
        self._tools = tuple(t for t in tools if t in _RUNNERS)
        self._timeout = max(5.0, float(timeout_per_tool_s))
        self._on_event = on_event

    async def scan(self, paths: list[str]) -> StaticSwarmResult:
        import time as _t
        if not paths:
            return StaticSwarmResult(
                findings=[], tools_run=[], tools_skipped=list(self._tools),
                elapsed_ms=0.0,
            )

        start = _t.monotonic()
        # Detect which tools are present; emit tool_skipped for the rest.
        runnable: list[tuple[str, Callable[..., Awaitable[list[Finding]]]]] = []
        skipped: list[str] = []
        for name in self._tools:
            spec = next((s for s in DEFAULT_TOOLS if s.name == name), None)
            if spec is None or not detect_tool(spec):
                skipped.append(name)
                await self._emit("tool_skipped", {"tool": name})
                continue
            runnable.append((name, _RUNNERS[name]))

        # Fire all available tools in parallel.
        tasks = [
            asyncio.create_task(self._safe_run(name, runner, paths))
            for name, runner in runnable
        ]
        per_tool = await asyncio.gather(*tasks, return_exceptions=False)
        all_findings: list[Finding] = []
        for batch in per_tool:
            all_findings.extend(batch)

        return StaticSwarmResult(
            findings=all_findings,
            tools_run=[n for n, _ in runnable],
            tools_skipped=skipped,
            elapsed_ms=(_t.monotonic() - start) * 1000.0,
        )

    async def _safe_run(
        self,
        name: str,
        runner: Callable[..., Awaitable[list[Finding]]],
        paths: list[str],
    ) -> list[Finding]:
        try:
            await self._emit("tool_started", {"tool": name})
            findings = await runner(paths, timeout_s=self._timeout)
            await self._emit("tool_completed", {
                "tool": name, "findings_count": len(findings),
            })
            return findings
        except Exception as exc:  # pragma: no cover - infra
            logger.debug("static tool %s failed: %s", name, exc)
            await self._emit("tool_failed", {
                "tool": name, "error": f"{type(exc).__name__}",
            })
            return []

    async def _emit(self, event: str, payload: dict[str, Any]) -> None:
        if self._on_event is None:
            return
        try:
            await self._on_event(event, payload)
        except Exception as exc:  # pragma: no cover
            logger.debug("static_swarm on_event(%s) failed: %s", event, exc)


__all__ = [
    "DEFAULT_TOOLS",
    "Finding",
    "StaticSwarm",
    "StaticSwarmResult",
    "ToolSpec",
    "detect_tool",
    "parse_bandit_json",
    "parse_cppcheck_xml",
    "parse_gitleaks_json",
    "parse_gosec_json",
    "parse_mypy_json",
    "parse_pylint_json",
    "parse_semgrep_json",
    "parse_trivy_json",
    "run_bandit",
    "run_cppcheck",
    "run_gitleaks",
    "run_gosec",
    "run_mypy",
    "run_pylint",
    "run_semgrep",
    "run_trivy",
]
