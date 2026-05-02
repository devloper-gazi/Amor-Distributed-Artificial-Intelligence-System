"""
Sentinel — typed contracts (dataclasses).

Mirrors the Consortium models pattern: dataclass-based artifacts
that serialise cleanly via ``asdict()`` for SSE / Mongo / artifact
zip.  ``Finding`` is the universal currency — every static tool,
ML stage, and LLM agent normalises its output into a list of
``Finding`` records.

License: MIT (matches repo).
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Literal


# ─────────────────────────────────────────────────────────────────────
# Enums (Literal types for stable JSON shape)
# ─────────────────────────────────────────────────────────────────────


SeverityLevel = Literal["info", "low", "medium", "high", "critical"]
"""Stable severity ladder.  ``info`` = noteworthy but not actionable;
``critical`` = exploitable in default config, fix immediately."""


ScanProfile = Literal["quick", "standard", "deep", "paranoid"]
"""User-selected scan profile.

* ``quick``     — static + ML only, ~30 s.
* ``standard``  — + Auditor + Patcher, ~3 min.
* ``deep``      — + Reasoner + RedTeam + Judge, ~10-15 min.
* ``paranoid``  — Deep + synthetic-injection self-test."""


SourceKind = Literal[
    "static_tool",   # bandit / semgrep / gitleaks / etc.
    "ml_classifier", # secret detector / anomaly / severity ranker
    "auditor",       # AuditorAgent voting result
    "reasoner",      # ReasonerAgent CoT
    "redteam",       # RedTeamAgent exploit simulation
    "patcher",       # Patcher's re-check
    "judge",         # Judge synthesis
]
"""Where a Finding originated.  Drives Bayesian source weights."""


AgentRole = Literal["auditor", "reasoner", "redteam", "patcher", "judge"]


# ─────────────────────────────────────────────────────────────────────
# Severity utilities
# ─────────────────────────────────────────────────────────────────────


_SEVERITY_RANK: dict[str, int] = {
    "info": 0, "low": 1, "medium": 2, "high": 3, "critical": 4,
}


def coerce_severity(value: Any, default: SeverityLevel = "low") -> SeverityLevel:
    """Tolerant cast of any input to a valid SeverityLevel.

    Accepts case-insensitive strings, common synonyms (``warning``
    → ``medium``, ``error`` → ``high``), and numeric ranks (0..4).
    Falls back to ``default`` on unknown input — never raises."""
    if value is None:
        return default
    if isinstance(value, (int, float)):
        rank = max(0, min(4, int(value)))
        return ("info", "low", "medium", "high", "critical")[rank]
    s = str(value).strip().lower()
    if s in _SEVERITY_RANK:
        return s  # type: ignore[return-value]
    aliases = {
        "warning": "medium",
        "warn":    "medium",
        "moderate": "medium",
        "error":   "high",
        "err":     "high",
        "fatal":   "critical",
        "blocker": "critical",
        "trivial": "info",
        "note":    "info",
        "minor":   "low",
    }
    return aliases.get(s, default)  # type: ignore[return-value]


def severity_rank(level: str) -> int:
    """0..4 ordering, useful for sort + Bayesian weight calc."""
    return _SEVERITY_RANK.get(coerce_severity(level), 1)


def coerce_scan_profile(
    value: Any, default: ScanProfile = "standard"
) -> ScanProfile:
    s = str(value or "").strip().lower()
    if s in ("quick", "standard", "deep", "paranoid"):
        return s  # type: ignore[return-value]
    return default


# ─────────────────────────────────────────────────────────────────────
# Finding — universal currency
# ─────────────────────────────────────────────────────────────────────


@dataclass
class Finding:
    """Normalised security finding.  Every tool / ML / LLM output is
    coerced into this shape so downstream phases don't care about
    provenance."""

    # ── Identity ──────────────────────────────────────────────────
    tool: str                          # "bandit" / "auditor" / ...
    source_kind: SourceKind = "static_tool"
    rule_id: str = ""                  # e.g. "B608", "CWE-89"

    # ── Location ─────────────────────────────────────────────────
    file: str = ""
    line_start: int = 0
    line_end: int = 0
    column_start: int = 0
    column_end: int = 0

    # ── Content ──────────────────────────────────────────────────
    raw_message: str = ""
    code_snippet: str = ""
    language: str = ""

    # ── Classification ───────────────────────────────────────────
    severity: SeverityLevel = "low"
    confidence: float = 0.5            # 0..1; per-source confidence
    cwe: str = ""                      # "CWE-89"
    cwe_name: str = ""
    owasp: str = ""                    # "A03:2021"
    cvss_base_score: float = 0.0       # 0..10 (CVSS 3.1 base)
    cvss_vector: str = ""              # "AV:N/AC:L/..."

    # ── Provenance & metadata ────────────────────────────────────
    source_weight: float = 0.5         # Bayesian weight per source
    extra: dict[str, Any] = field(default_factory=dict)
    fingerprint: str = ""              # stable hash for dedup

    # ── Static helpers ───────────────────────────────────────────

    def __post_init__(self) -> None:
        # Force severity into the canonical Literal set.
        self.severity = coerce_severity(self.severity, default="low")
        self.confidence = max(0.0, min(1.0, float(self.confidence or 0.0)))
        self.source_weight = max(0.0, min(1.0, float(self.source_weight or 0.0)))
        # Ensure line numbers are non-negative ints.
        self.line_start = max(0, int(self.line_start or 0))
        self.line_end = max(self.line_start, int(self.line_end or self.line_start))
        if not self.fingerprint:
            self.fingerprint = self._compute_fingerprint()

    def _compute_fingerprint(self) -> str:
        import hashlib
        # (file, line_start, cwe or rule_id) is the canonical merge key.
        # Hashing instead of cat-string keeps the fingerprint short.
        key = f"{self.file}|{self.line_start}|{self.cwe or self.rule_id}"
        return hashlib.sha256(key.encode("utf-8", "replace")).hexdigest()[:16]

    def merge_key(self) -> tuple[str, int, str]:
        """Triple used for Bayesian merging across sources."""
        return (self.file, self.line_start, self.cwe or self.rule_id or "")

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Finding":
        # Resilient mapping — older static-tool dumps may miss fields.
        return cls(
            tool=str(d.get("tool") or "unknown"),
            source_kind=d.get("source_kind") or "static_tool",  # type: ignore[arg-type]
            rule_id=str(d.get("rule_id") or ""),
            file=str(d.get("file") or ""),
            line_start=int(d.get("line_start") or 0),
            line_end=int(d.get("line_end") or d.get("line_start") or 0),
            column_start=int(d.get("column_start") or 0),
            column_end=int(d.get("column_end") or 0),
            raw_message=str(d.get("raw_message") or "")[:4000],
            code_snippet=str(d.get("code_snippet") or "")[:4000],
            language=str(d.get("language") or ""),
            severity=coerce_severity(d.get("severity"), default="low"),
            confidence=float(d.get("confidence") or 0.5),
            cwe=str(d.get("cwe") or ""),
            cwe_name=str(d.get("cwe_name") or ""),
            owasp=str(d.get("owasp") or ""),
            cvss_base_score=float(d.get("cvss_base_score") or 0.0),
            cvss_vector=str(d.get("cvss_vector") or ""),
            source_weight=float(d.get("source_weight") or 0.5),
            extra=dict(d.get("extra") or {}),
            fingerprint=str(d.get("fingerprint") or ""),
        )


# ─────────────────────────────────────────────────────────────────────
# AgentVerdict — what each LLM agent returns
# ─────────────────────────────────────────────────────────────────────


@dataclass
class AgentVerdict:
    """Single LLM agent's call result."""

    role: AgentRole
    verdict: Literal["true_positive", "false_positive", "needs_more_context", "exploitable", "not_exploitable", "approved", "rejected"] = "needs_more_context"
    confidence: float = 0.5
    rationale: str = ""
    suggested_severity: SeverityLevel = "low"
    cwe: str = ""
    exploit_scenario: str = ""        # populated by RedTeam
    fix_diff: str = ""                # populated by Patcher
    raw_llm_output: str = ""          # full LLM response, for debugging
    elapsed_ms: float = 0.0

    def __post_init__(self) -> None:
        self.confidence = max(0.0, min(1.0, float(self.confidence or 0.0)))
        self.suggested_severity = coerce_severity(
            self.suggested_severity, default="low"
        )

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        # raw_llm_output can be hundreds of KB — keep it on the
        # dataclass for debugging but skip it from public dict.
        d.pop("raw_llm_output", None)
        return d


# ─────────────────────────────────────────────────────────────────────
# ConfidenceScore — Bayesian merge result
# ─────────────────────────────────────────────────────────────────────


@dataclass
class ConfidenceScore:
    """Merged confidence across multiple sources for a single
    ``(file, line, cwe)`` tuple."""

    final: float = 0.0
    breakdown: dict[str, float] = field(default_factory=dict)  # source → confidence
    sources_count: int = 0

    def __post_init__(self) -> None:
        self.final = max(0.0, min(1.0, float(self.final or 0.0)))
        self.sources_count = max(0, int(self.sources_count or 0))

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# ─────────────────────────────────────────────────────────────────────
# RAGContext — what the corpus retriever returns
# ─────────────────────────────────────────────────────────────────────


@dataclass
class RAGContext:
    """Context fetched for a single Finding from CWE / OWASP /
    history corpora."""

    cwe_entry: dict[str, Any] | None = None
    owasp_entry: dict[str, Any] | None = None
    similar_findings: list[dict[str, Any]] = field(default_factory=list)
    project_chunks: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# ─────────────────────────────────────────────────────────────────────
# SentinelGate — per-phase quality gate (mirrors VerificationGate)
# ─────────────────────────────────────────────────────────────────────


@dataclass
class SentinelGate:
    phase: str
    status: Literal["passed", "passed_warn", "failed", "skipped"]
    score: float = 0.0
    findings_count: int = 0
    summary: str = ""
    detail: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# ─────────────────────────────────────────────────────────────────────
# Request / Bundle
# ─────────────────────────────────────────────────────────────────────


@dataclass
class SentinelRequest:
    """Input contract for ``SentinelEngine.run()``."""

    prompt: str = ""                     # human-readable goal, optional
    paths: list[str] = field(default_factory=list)
    code_context: str | None = None      # raw code paste alternative
    language: str | None = None
    scan_profile: ScanProfile = "standard"
    cancel_requested: bool = False
    # Optional toggles — engine reads these or settings, request wins.
    enable_static_swarm: bool | None = None
    enable_ml_pipeline: bool | None = None
    enable_rag: bool | None = None
    enable_critic_loop: bool | None = None
    enable_self_play: bool | None = None

    def normalize(self) -> "SentinelRequest":
        self.scan_profile = coerce_scan_profile(self.scan_profile)
        # Drop empty paths + dedup while preserving order.
        seen: set[str] = set()
        cleaned: list[str] = []
        for p in self.paths or []:
            s = str(p or "").strip()
            if not s or s in seen:
                continue
            seen.add(s)
            cleaned.append(s)
        self.paths = cleaned
        if not self.paths and not (self.code_context or "").strip():
            # Engine will surface an error; we don't raise here so the
            # API layer can return a clean 400 instead of a 500.
            pass
        return self

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class SentinelBundle:
    """Everything ``SentinelEngine.run()`` returns."""

    session_id: str = ""
    request: SentinelRequest = field(default_factory=SentinelRequest)

    # Per-stage outputs
    static_findings: list[Finding] = field(default_factory=list)
    ml_findings: list[Finding] = field(default_factory=list)
    agent_verdicts: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    # ^ keyed by AgentRole, each value is list of AgentVerdict.to_dict()

    # Final merged set (after Bayesian merge + critic loop)
    findings: list[Finding] = field(default_factory=list)

    # Aggregate scores
    repo_risk_score: float = 0.0          # 0..10
    severity_histogram: dict[str, int] = field(default_factory=dict)
    confidence_summary: dict[str, Any] = field(default_factory=dict)

    # Pipeline gates
    gates: list[SentinelGate] = field(default_factory=list)

    # Reports — populated by reporters phase
    sarif_report: str = ""
    markdown_report: str = ""
    html_report: str = ""
    artifact_path: str = ""

    # Bookkeeping
    models_used: dict[str, str] = field(default_factory=dict)
    started_at: str = ""
    completed_at: str = ""
    elapsed_ms: float = 0.0
    debate_log: list[dict[str, Any]] = field(default_factory=list)
    critic_iterations: int = 0
    tool_skipped: list[str] = field(default_factory=list)

    def severity_count(self, level: SeverityLevel) -> int:
        return int(self.severity_histogram.get(level, 0))

    def to_dict(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "request": self.request.to_dict(),
            "static_findings": [f.to_dict() for f in self.static_findings],
            "ml_findings": [f.to_dict() for f in self.ml_findings],
            "agent_verdicts": {
                role: list(records) for role, records in self.agent_verdicts.items()
            },
            "findings": [f.to_dict() for f in self.findings],
            "repo_risk_score": round(float(self.repo_risk_score), 3),
            "severity_histogram": dict(self.severity_histogram),
            "confidence_summary": dict(self.confidence_summary),
            "gates": [g.to_dict() for g in self.gates],
            "sarif_report": self.sarif_report,
            "markdown_report": self.markdown_report,
            "html_report": self.html_report,
            "artifact_path": self.artifact_path,
            "models_used": dict(self.models_used),
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "elapsed_ms": float(self.elapsed_ms),
            "debate_log": list(self.debate_log),
            "critic_iterations": int(self.critic_iterations),
            "tool_skipped": list(self.tool_skipped),
        }


# ─────────────────────────────────────────────────────────────────────
# Public API
# ─────────────────────────────────────────────────────────────────────


__all__ = [
    "AgentRole",
    "AgentVerdict",
    "ConfidenceScore",
    "Finding",
    "RAGContext",
    "ScanProfile",
    "SentinelBundle",
    "SentinelGate",
    "SentinelRequest",
    "SeverityLevel",
    "SourceKind",
    "coerce_scan_profile",
    "coerce_severity",
    "severity_rank",
]
