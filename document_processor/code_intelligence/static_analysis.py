"""
StaticAnalysisHarness — runs AST, lint, type, complexity, and security
checks on code snippets without executing them.

All external analysers (pylint, mypy, bandit, radon) are optional —
the harness degrades gracefully when a tool is missing or times out.
The point is to give the LLM richer feedback than execution alone:
type warnings and complexity hotspots can flag bugs that pass the
test suite by chance.
"""

from __future__ import annotations

import ast
import asyncio
import json
import logging
import os
import sys
import tempfile
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# Result dataclasses
# ─────────────────────────────────────────────────────────────────────────────


@dataclass
class AnalysisIssue:
    severity: str           # "error" | "warning" | "info" | "security"
    code: str               # Tool-specific code, e.g. "E501", "B101"
    message: str
    line: Optional[int] = None
    col: Optional[int] = None
    source: str = "unknown"  # "pylint" | "mypy" | "ast" | "bandit" | "radon"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "severity": self.severity,
            "code": self.code,
            "message": self.message,
            "line": self.line,
            "col": self.col,
            "source": self.source,
        }


@dataclass
class StaticAnalysisResult:
    language: str
    issues: List[AnalysisIssue] = field(default_factory=list)
    complexity_score: Optional[float] = None
    maintainability_index: Optional[float] = None
    lines_of_code: int = 0
    syntax_valid: bool = True
    syntax_error: Optional[str] = None
    ast_summary: Optional[Dict[str, Any]] = None

    def severity_counts(self) -> Dict[str, int]:
        counts: Dict[str, int] = {
            "error": 0, "warning": 0, "info": 0, "security": 0,
        }
        for issue in self.issues:
            counts[issue.severity] = counts.get(issue.severity, 0) + 1
        return counts

    def to_dict(self) -> Dict[str, Any]:
        return {
            "language": self.language,
            "issues": [i.to_dict() for i in self.issues],
            "complexity_score": self.complexity_score,
            "maintainability_index": self.maintainability_index,
            "lines_of_code": self.lines_of_code,
            "syntax_valid": self.syntax_valid,
            "syntax_error": self.syntax_error,
            "ast_summary": self.ast_summary,
            "severity_counts": self.severity_counts(),
        }

    def to_feedback_str(self) -> str:
        counts = self.severity_counts()
        lines = [
            f"Static Analysis: {counts['error']} errors, "
            f"{counts['warning']} warnings, "
            f"{counts['info']} info, "
            f"{counts['security']} security issues"
        ]
        if not self.syntax_valid:
            lines.append(f"SYNTAX ERROR: {self.syntax_error}")
        if self.complexity_score is not None:
            lines.append(
                f"Avg cyclomatic complexity: {self.complexity_score:.1f}"
            )
        for issue in self.issues[:15]:
            loc = f"L{issue.line}" if issue.line else "?"
            lines.append(
                f"  [{issue.severity.upper()}] {loc} "
                f"{issue.code}: {issue.message}"
            )
        if len(self.issues) > 15:
            lines.append(f"  ... and {len(self.issues) - 15} more issues")
        return "\n".join(lines)


# ─────────────────────────────────────────────────────────────────────────────
# Harness
# ─────────────────────────────────────────────────────────────────────────────


class StaticAnalysisHarness:
    """Tries every analyser available; fails open on missing tools."""

    async def analyze(
        self,
        code: str,
        language: str = "python",
    ) -> StaticAnalysisResult:
        lang = (language or "python").lower().strip()
        if lang == "python":
            return await self._analyze_python(code)
        # Minimal result for non-Python languages — extend as needed.
        return StaticAnalysisResult(
            language=lang,
            lines_of_code=len(
                [l for l in code.splitlines() if l.strip()]
            ),
        )

    async def _analyze_python(self, code: str) -> StaticAnalysisResult:
        result = StaticAnalysisResult(language="python")
        result.lines_of_code = len(
            [l for l in code.splitlines() if l.strip()]
        )

        # 1. AST — syntax check + structural summary.
        try:
            tree = ast.parse(code)
            result.syntax_valid = True
            result.ast_summary = self._ast_summary(tree)
        except SyntaxError as exc:
            result.syntax_valid = False
            result.syntax_error = f"Line {exc.lineno}: {exc.msg}"
            result.issues.append(AnalysisIssue(
                severity="error",
                code="SyntaxError",
                message=result.syntax_error,
                line=exc.lineno,
                source="ast",
            ))
            # No point running further analysers on broken code.
            return result

        # External analysers all want a real file path.
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".py", delete=False, encoding="utf-8",
        ) as f:
            f.write(code)
            tmp_path = f.name

        try:
            await asyncio.gather(
                self._run_pylint(tmp_path, result),
                self._run_mypy(tmp_path, result),
                self._run_bandit(tmp_path, result),
                self._run_radon(tmp_path, result),
                return_exceptions=True,
            )
        finally:
            try:
                os.unlink(tmp_path)
            except Exception:
                pass

        return result

    # ── AST summary ───────────────────────────────────────────────────────

    @staticmethod
    def _ast_summary(tree: ast.AST) -> Dict[str, Any]:
        functions: List[Dict[str, Any]] = []
        classes: List[Dict[str, Any]] = []
        imports: List[str] = []
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                functions.append({
                    "name": node.name,
                    "line": node.lineno,
                    "args": [a.arg for a in node.args.args],
                    "is_async": isinstance(node, ast.AsyncFunctionDef),
                    "decorators": [
                        StaticAnalysisHarness._safe_unparse(d)
                        for d in node.decorator_list
                    ],
                })
            elif isinstance(node, ast.ClassDef):
                classes.append({
                    "name": node.name,
                    "line": node.lineno,
                    "bases": [
                        StaticAnalysisHarness._safe_unparse(b)
                        for b in node.bases
                    ],
                })
            elif isinstance(node, ast.Import):
                imports.extend(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                imports.append(f"from {node.module or '?'} import ...")
        return {
            "functions": functions[:20],
            "classes": classes[:10],
            "imports": list(dict.fromkeys(imports))[:20],
            "function_count": len(functions),
            "class_count": len(classes),
        }

    @staticmethod
    def _safe_unparse(node: ast.AST) -> str:
        try:
            return ast.unparse(node)  # py3.9+
        except Exception:
            return getattr(node, "id", node.__class__.__name__)

    # ── Pylint ────────────────────────────────────────────────────────────

    async def _run_pylint(
        self, path: str, result: StaticAnalysisResult,
    ) -> None:
        try:
            proc = await asyncio.create_subprocess_exec(
                sys.executable, "-m", "pylint", path,
                "--output-format=json", "--score=no",
                # Suppress docstring warnings; they're noise for snippets.
                "--disable=C0114,C0115,C0116",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
            stdout, _ = await asyncio.wait_for(
                proc.communicate(), timeout=30,
            )
            if not stdout.strip():
                return
            sev_map = {
                "E": "error", "F": "error",
                "W": "warning",
                "C": "info", "R": "info", "I": "info",
            }
            for item in json.loads(stdout.decode()):
                t = (item.get("type") or "W")[0].upper()
                result.issues.append(AnalysisIssue(
                    severity=sev_map.get(t, "info"),
                    code=str(item.get("message-id") or "?"),
                    message=str(item.get("message") or "")[:500],
                    line=item.get("line"),
                    col=item.get("column"),
                    source="pylint",
                ))
        except (FileNotFoundError, asyncio.TimeoutError, json.JSONDecodeError):
            pass
        except Exception as exc:  # pragma: no cover
            logger.debug("pylint_failed: %s", exc)

    # ── Mypy ──────────────────────────────────────────────────────────────

    async def _run_mypy(
        self, path: str, result: StaticAnalysisResult,
    ) -> None:
        try:
            proc = await asyncio.create_subprocess_exec(
                sys.executable, "-m", "mypy", path,
                "--ignore-missing-imports",
                "--no-error-summary",
                "--show-column-numbers",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
            stdout, _ = await asyncio.wait_for(
                proc.communicate(), timeout=30,
            )
            # Mypy text output: <path>:<line>:<col>: <severity>: <msg>  [code]
            import re
            line_re = re.compile(
                r"^[^:]+:(\d+):(?:(\d+):)?\s*(\w+):\s*(.+?)"
                r"(?:\s*\[([^\]]+)\])?\s*$"
            )
            for raw in stdout.decode(errors="replace").splitlines():
                m = line_re.match(raw.strip())
                if not m:
                    continue
                line_no = int(m.group(1))
                col_no = int(m.group(2)) if m.group(2) else None
                severity_raw = (m.group(3) or "warning").lower()
                msg = m.group(4) or ""
                code = m.group(5) or "mypy"
                result.issues.append(AnalysisIssue(
                    severity="error" if severity_raw == "error" else "warning",
                    code=code,
                    message=msg[:500],
                    line=line_no,
                    col=col_no,
                    source="mypy",
                ))
        except (FileNotFoundError, asyncio.TimeoutError):
            pass
        except Exception as exc:  # pragma: no cover
            logger.debug("mypy_failed: %s", exc)

    # ── Bandit ────────────────────────────────────────────────────────────

    async def _run_bandit(
        self, path: str, result: StaticAnalysisResult,
    ) -> None:
        try:
            proc = await asyncio.create_subprocess_exec(
                sys.executable, "-m", "bandit", "-f", "json", "-q", path,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
            stdout, _ = await asyncio.wait_for(
                proc.communicate(), timeout=20,
            )
            if not stdout.strip():
                return
            data = json.loads(stdout.decode())
            severity_map = {
                "HIGH": "security",
                "MEDIUM": "security",
                "LOW": "warning",
            }
            for issue in data.get("results", []):
                result.issues.append(AnalysisIssue(
                    severity=severity_map.get(
                        str(issue.get("issue_severity", "")).upper(),
                        "warning",
                    ),
                    code=str(issue.get("test_id") or "B???"),
                    message=(
                        f"{issue.get('issue_text', '')} "
                        f"(confidence: {issue.get('issue_confidence', '?')})"
                    )[:500],
                    line=issue.get("line_number"),
                    source="bandit",
                ))
        except (FileNotFoundError, asyncio.TimeoutError, json.JSONDecodeError):
            pass
        except Exception as exc:  # pragma: no cover
            logger.debug("bandit_failed: %s", exc)

    # ── Radon (cyclomatic complexity) ─────────────────────────────────────

    async def _run_radon(
        self, path: str, result: StaticAnalysisResult,
    ) -> None:
        try:
            proc = await asyncio.create_subprocess_exec(
                sys.executable, "-m", "radon", "cc", path, "-j",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
            stdout, _ = await asyncio.wait_for(
                proc.communicate(), timeout=15,
            )
            if not stdout.strip():
                return
            data = json.loads(stdout.decode())
            complexities: List[int] = []
            for blocks in data.values():
                if not isinstance(blocks, list):
                    continue
                for block in blocks:
                    c = int(block.get("complexity", 0) or 0)
                    complexities.append(c)
                    if c >= 10:
                        result.issues.append(AnalysisIssue(
                            severity="warning",
                            code=f"CC{c}",
                            message=(
                                f"High cyclomatic complexity ({c}) in "
                                f"'{block.get('name', '?')}'"
                            ),
                            line=block.get("lineno"),
                            source="radon",
                        ))
            if complexities:
                result.complexity_score = (
                    sum(complexities) / len(complexities)
                )
        except (FileNotFoundError, asyncio.TimeoutError, json.JSONDecodeError):
            pass
        except Exception as exc:  # pragma: no cover
            logger.debug("radon_failed: %s", exc)
