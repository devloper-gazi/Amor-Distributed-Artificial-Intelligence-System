"""
Sentinel — reporters (SARIF 2.1.0, Markdown, HTML).

All three formats produced from the same SentinelBundle so they
stay consistent.  Output strings are returned + written to disk
when an artifact_root is supplied; the engine zips them at the
end of a run.

License: MIT.
"""

from __future__ import annotations

import datetime as _dt
import html
import json
import logging
from pathlib import Path
from typing import Any, Iterable

from .models import Finding, SentinelBundle, SeverityLevel, severity_rank

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────
# SARIF 2.1.0 reporter
# ─────────────────────────────────────────────────────────────────────


_SARIF_LEVEL_MAP: dict[SeverityLevel, str] = {
    "info": "note",
    "low": "note",
    "medium": "warning",
    "high": "error",
    "critical": "error",
}


class SARIFReporter:
    """Emit a minimal-but-valid SARIF 2.1.0 document."""

    SCHEMA_URI = (
        "https://raw.githubusercontent.com/oasis-tcs/sarif-spec/master/"
        "Schemata/sarif-schema-2.1.0.json"
    )
    SCHEMA_VERSION = "2.1.0"

    TOOL_NAME = "sentinel"
    TOOL_VERSION = "1.0.0"
    TOOL_INFORMATION_URI = (
        "https://github.com/devloper-gazi/Amor-Distributed-Artificial-Intelligence-System"
    )

    def render(self, bundle: SentinelBundle) -> str:
        runs = [self._build_run(bundle)]
        document = {
            "$schema": self.SCHEMA_URI,
            "version": self.SCHEMA_VERSION,
            "runs": runs,
        }
        return json.dumps(document, default=str, indent=2)

    def write(self, bundle: SentinelBundle, path: Path) -> Path:
        text = self.render(bundle)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return path

    # ─── Internals ──────────────────────────────────────────────

    def _build_run(self, bundle: SentinelBundle) -> dict[str, Any]:
        rules = self._build_rules(bundle.findings)
        results = [self._build_result(f) for f in bundle.findings]
        return {
            "tool": {
                "driver": {
                    "name": self.TOOL_NAME,
                    "version": self.TOOL_VERSION,
                    "informationUri": self.TOOL_INFORMATION_URI,
                    "rules": rules,
                }
            },
            "invocations": [
                {
                    "executionSuccessful": True,
                    "startTimeUtc": bundle.started_at or _now_iso(),
                    "endTimeUtc": bundle.completed_at or _now_iso(),
                }
            ],
            "results": results,
        }

    def _build_rules(self, findings: Iterable[Finding]) -> list[dict[str, Any]]:
        seen: dict[str, dict[str, Any]] = {}
        for f in findings:
            rid = f.rule_id or f.cwe or "sentinel.unknown"
            if rid in seen:
                continue
            seen[rid] = {
                "id": rid,
                "name": rid,
                "shortDescription": {"text": f.cwe_name or rid},
                "fullDescription": {
                    "text": (f.raw_message[:200] or rid)
                },
                "defaultConfiguration": {
                    "level": _SARIF_LEVEL_MAP.get(f.severity, "warning"),
                },
                "properties": {
                    "tool": f.tool,
                    "cwe": f.cwe,
                    "owasp": f.owasp,
                },
            }
        return list(seen.values())

    def _build_result(self, f: Finding) -> dict[str, Any]:
        return {
            "ruleId": f.rule_id or f.cwe or "sentinel.unknown",
            "level": _SARIF_LEVEL_MAP.get(f.severity, "warning"),
            "message": {"text": f.raw_message[:1000] or "(no message)"},
            "locations": [
                {
                    "physicalLocation": {
                        "artifactLocation": {"uri": f.file or "unknown"},
                        "region": {
                            "startLine": max(1, f.line_start),
                            "endLine": max(1, f.line_end or f.line_start),
                        },
                    }
                }
            ],
            "properties": {
                "tool": f.tool,
                "source_kind": f.source_kind,
                "confidence": f.confidence,
                "cwe": f.cwe,
                "owasp": f.owasp,
                "cvss_base_score": f.cvss_base_score,
                "fingerprint": f.fingerprint,
            },
        }


# ─────────────────────────────────────────────────────────────────────
# Markdown reporter
# ─────────────────────────────────────────────────────────────────────


class MarkdownReporter:
    def render(self, bundle: SentinelBundle) -> str:
        rows: list[str] = []
        rows.append("# Sentinel Security Report\n")
        rows.append(f"- **Session**: `{bundle.session_id or '-'}`")
        rows.append(f"- **Profile**: `{bundle.request.scan_profile}`")
        rows.append(f"- **Started**: `{bundle.started_at or '-'}`")
        rows.append(f"- **Completed**: `{bundle.completed_at or '-'}`")
        rows.append(f"- **Repo risk score**: **{bundle.repo_risk_score:.1f} / 10**")
        rows.append("")
        rows.append("## Severity histogram")
        rows.append("")
        rows.append("| Level | Count |")
        rows.append("|---|---|")
        for level in ("critical", "high", "medium", "low", "info"):
            rows.append(f"| {level} | {bundle.severity_count(level)} |")
        rows.append("")
        if bundle.tool_skipped:
            rows.append("> **Tools skipped** (binary not on $PATH): "
                        f"{', '.join(sorted(set(bundle.tool_skipped)))}")
            rows.append("")
        rows.append("## Findings")
        rows.append("")
        # Sorted by severity descending then by confidence
        sorted_f = sorted(
            bundle.findings,
            key=lambda f: (severity_rank(f.severity), f.confidence),
            reverse=True,
        )
        for f in sorted_f:
            rows.append(self._render_finding(f))
            rows.append("")
        if not sorted_f:
            rows.append("_No findings — repo is clean (per the configured profile)._")
        return "\n".join(rows) + "\n"

    def write(self, bundle: SentinelBundle, path: Path) -> Path:
        text = self.render(bundle)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return path

    def _render_finding(self, f: Finding) -> str:
        level = f.severity.upper()
        cwe = f.cwe or "—"
        owasp = f.owasp or "—"
        rows = [
            f"### {level} · `{f.tool}` · {f.rule_id or cwe}",
            "",
            f"- **File**: `{f.file}` line **{f.line_start}**",
            f"- **CWE**: `{cwe}`  &nbsp; **OWASP**: `{owasp}`",
            f"- **Confidence**: {f.confidence:.2f}  "
            f"&nbsp; **CVSS**: {f.cvss_base_score:.1f}",
            "",
            "**Message**",
            "",
            f"> {f.raw_message[:600]}",
        ]
        if f.code_snippet and f.code_snippet != "<redacted>":
            rows.append("")
            rows.append("```")
            rows.append(f.code_snippet[:800])
            rows.append("```")
        return "\n".join(rows)


# ─────────────────────────────────────────────────────────────────────
# HTML reporter (CSP-strict)
# ─────────────────────────────────────────────────────────────────────


_HTML_TEMPLATE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline'; img-src data:; font-src data:;">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Sentinel Report — {session}</title>
<style>
:root{{--bg:#0e1116;--fg:#e6edf3;--muted:#8b949e;--card:#161b22;--border:#30363d;--accent:#58a6ff;--crit:#f85149;--high:#ff7b72;--med:#d29922;--low:#7ee787;--info:#8b949e;}}
body{{margin:0;background:var(--bg);color:var(--fg);font:14px/1.5 ui-sans-serif,system-ui,-apple-system,sans-serif;}}
.wrap{{max-width:1080px;margin:0 auto;padding:24px;}}
h1{{margin:0 0 8px;font-size:24px;}}
.meta{{color:var(--muted);margin-bottom:16px;}}
.histogram{{display:flex;gap:12px;margin:16px 0 24px;flex-wrap:wrap;}}
.bucket{{background:var(--card);border:1px solid var(--border);border-radius:10px;padding:8px 12px;}}
.bucket strong{{display:block;font-size:18px;}}
.bucket.crit{{border-color:var(--crit);}}
.bucket.high{{border-color:var(--high);}}
.bucket.med{{border-color:var(--med);}}
.bucket.low{{border-color:var(--low);}}
.bucket.info{{border-color:var(--info);}}
.skipped{{color:var(--muted);font-style:italic;margin-bottom:16px;}}
.finding{{background:var(--card);border:1px solid var(--border);border-radius:10px;padding:14px 18px;margin-bottom:14px;}}
.finding h3{{margin:0 0 8px;font-size:15px;}}
.tag{{display:inline-block;font-size:10.5px;font-weight:700;padding:2px 7px;border-radius:999px;text-transform:uppercase;letter-spacing:0.04em;margin-right:6px;}}
.tag.crit{{background:rgba(248,81,73,0.18);color:var(--crit);}}
.tag.high{{background:rgba(255,123,114,0.18);color:var(--high);}}
.tag.med{{background:rgba(210,153,34,0.18);color:var(--med);}}
.tag.low{{background:rgba(126,231,135,0.18);color:var(--low);}}
.tag.info{{background:rgba(139,148,158,0.18);color:var(--info);}}
.row{{display:flex;flex-wrap:wrap;gap:14px;color:var(--muted);font-size:13px;margin-bottom:6px;}}
.row code{{color:var(--fg);}}
pre{{background:#0a0d12;border:1px solid var(--border);border-radius:8px;padding:10px;overflow-x:auto;font-size:12.5px;}}
footer{{margin-top:36px;color:var(--muted);font-size:12px;}}
</style>
</head>
<body>
<div class="wrap">
<h1>Sentinel Security Report</h1>
<div class="meta">Session <code>{session}</code> · Profile <code>{profile}</code> · Risk <strong>{risk:.1f} / 10</strong> · {ftotal} findings</div>
<div class="histogram">
{histogram}
</div>
{skipped_block}
<section>
{findings}
</section>
<footer>Generated by Sentinel V1 — 100% local, zero telemetry. Started {started}, completed {completed}.</footer>
</div>
</body>
</html>
"""


_SEV_CLASS: dict[SeverityLevel, str] = {
    "critical": "crit", "high": "high", "medium": "med",
    "low": "low", "info": "info",
}


class HTMLReporter:
    def render(self, bundle: SentinelBundle) -> str:
        ftotal = len(bundle.findings)
        histogram_html = "".join(
            f'<div class="bucket {_SEV_CLASS[level]}"><strong>'
            f'{bundle.severity_count(level)}</strong>{level}</div>'
            for level in ("critical", "high", "medium", "low", "info")
        )
        skipped_block = ""
        if bundle.tool_skipped:
            skipped_block = (
                '<div class="skipped">Tools skipped: '
                + html.escape(", ".join(sorted(set(bundle.tool_skipped))))
                + "</div>"
            )
        sorted_f = sorted(
            bundle.findings,
            key=lambda f: (severity_rank(f.severity), f.confidence),
            reverse=True,
        )
        findings_html = "\n".join(self._render_finding(f) for f in sorted_f) \
            or '<div class="skipped">No findings — repo is clean.</div>'
        return _HTML_TEMPLATE.format(
            session=html.escape(bundle.session_id or "-"),
            profile=html.escape(bundle.request.scan_profile),
            risk=bundle.repo_risk_score,
            ftotal=ftotal,
            histogram=histogram_html,
            skipped_block=skipped_block,
            findings=findings_html,
            started=html.escape(bundle.started_at or "-"),
            completed=html.escape(bundle.completed_at or "-"),
        )

    def write(self, bundle: SentinelBundle, path: Path) -> Path:
        text = self.render(bundle)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return path

    def _render_finding(self, f: Finding) -> str:
        level = f.severity
        cls = _SEV_CLASS.get(level, "info")
        snippet_html = ""
        if f.code_snippet and f.code_snippet != "<redacted>":
            snippet_html = f"<pre>{html.escape(f.code_snippet[:800])}</pre>"
        return (
            f'<div class="finding">'
            f'<h3><span class="tag {cls}">{level}</span>'
            f'{html.escape(f.tool)} · {html.escape(f.rule_id or f.cwe or "rule")}</h3>'
            f'<div class="row">File <code>{html.escape(f.file or "unknown")}</code> '
            f'line <code>{f.line_start}</code></div>'
            f'<div class="row">CWE <code>{html.escape(f.cwe or "—")}</code> · '
            f'OWASP <code>{html.escape(f.owasp or "—")}</code> · '
            f'Confidence <code>{f.confidence:.2f}</code> · '
            f'CVSS <code>{f.cvss_base_score:.1f}</code></div>'
            f'<div>{html.escape(f.raw_message[:1200])}</div>'
            f'{snippet_html}'
            f'</div>'
        )


# ─────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────


def _now_iso() -> str:
    return _dt.datetime.now(_dt.UTC).isoformat()


__all__ = ["HTMLReporter", "MarkdownReporter", "SARIFReporter"]
