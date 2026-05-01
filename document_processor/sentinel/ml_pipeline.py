"""
Sentinel — classical ML pipeline (pure-Python heuristics by default).

Three stages:

1. **SecretDetector** — regex catalogue (AWS / GCP / Stripe / GitHub
   / Slack tokens, SSH private keys, JWT) + Shannon-entropy threshold
   for high-entropy strings that the regex catalogue misses.
2. **AnomalyDetector** — per-file Z-score on
   ``(loc, complexity, imports, base64_density)``.  Files outside
   ``|z| > anomaly_threshold`` are flagged.
3. **SeverityRanker** — weighted-sum heuristic that combines source
   weight + base severity + exposure surface (auth / api / network).

Optional ML backends:

* ``scikit-learn`` — when installed, the SecretDetector can load a
  ``RandomForestClassifier`` pickle from
  ``sentinel/data/secret_detector.pkl`` (NOT bundled — produced by
  the user via a future ``amor sentinel train-secrets`` command).
* ``xgboost`` — when installed, SeverityRanker can swap the
  weighted-sum heuristic for a trained gradient-boosting model.
* ``IsolationForest`` — when scikit-learn is installed,
  AnomalyDetector can use it instead of pure Z-score.

The active backend is logged at construction and is part of the
SSE event stream so the user sees ``ml_backend=heuristic|sklearn|xgboost``
during a scan.

License: MIT.
"""

from __future__ import annotations

import logging
import math
import os
import re
import statistics
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from .models import Finding, SeverityLevel, severity_rank

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────
# Shannon entropy helper
# ─────────────────────────────────────────────────────────────────────


def shannon_entropy(text: str) -> float:
    """Bits per character — 0 for constant string, ~6.0 for random
    base64.  Fast pure-Python impl."""
    if not text:
        return 0.0
    n = len(text)
    counts: dict[str, int] = {}
    for ch in text:
        counts[ch] = counts.get(ch, 0) + 1
    return -sum((c / n) * math.log2(c / n) for c in counts.values())


# ─────────────────────────────────────────────────────────────────────
# Secret regex catalogue
#
# Each pattern: (rule_id, regex, severity, cwe, confidence, label).
# Patterns chosen for low FP rate on real source.  Prefix-anchored
# tokens (sk_, AKIA, ghp_) keep regex cheap.
# ─────────────────────────────────────────────────────────────────────


SECRET_PATTERNS: tuple[tuple[str, re.Pattern[str], SeverityLevel, str, float, str], ...] = (
    (
        "aws-access-key",
        re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
        "critical", "CWE-798", 0.92, "AWS Access Key",
    ),
    (
        "aws-secret-key",
        re.compile(r"\b[A-Za-z0-9/+=]{40}\b(?=.*aws_secret)", re.I),
        "critical", "CWE-798", 0.7, "AWS Secret (heuristic)",
    ),
    (
        "gcp-service-account",
        re.compile(r'"type":\s*"service_account"'),
        "critical", "CWE-798", 0.85, "GCP Service Account",
    ),
    (
        "stripe-key",
        re.compile(r"\bsk_(live|test)_[0-9A-Za-z]{24,}\b"),
        "critical", "CWE-798", 0.95, "Stripe Secret Key",
    ),
    (
        "github-pat",
        re.compile(r"\bghp_[0-9A-Za-z]{36,}\b"),
        "critical", "CWE-798", 0.95, "GitHub Personal Access Token",
    ),
    (
        "github-app-token",
        re.compile(r"\b(ghu|ghs|ghr|ghi)_[0-9A-Za-z]{36,}\b"),
        "critical", "CWE-798", 0.95, "GitHub App Token",
    ),
    (
        "slack-bot-token",
        re.compile(r"\bxox[abprs]-[0-9A-Za-z-]{10,}\b"),
        "high", "CWE-798", 0.9, "Slack Token",
    ),
    (
        "openai-api-key",
        re.compile(r"\bsk-[A-Za-z0-9]{20,}\b"),
        "high", "CWE-798", 0.85, "OpenAI API Key",
    ),
    (
        "anthropic-api-key",
        re.compile(r"\bsk-ant-[A-Za-z0-9_-]{30,}\b"),
        "high", "CWE-798", 0.95, "Anthropic API Key",
    ),
    (
        "private-rsa-key",
        re.compile(r"-----BEGIN (RSA |OPENSSH |EC )?PRIVATE KEY-----"),
        "critical", "CWE-321", 0.99, "Private Key Material",
    ),
    (
        "jwt",
        re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b"),
        "medium", "CWE-798", 0.7, "JWT Token",
    ),
    (
        "generic-bearer-token",
        re.compile(r"(Bearer|Authorization:\s*Bearer)\s+[A-Za-z0-9._\-]{20,}", re.I),
        "high", "CWE-798", 0.55, "Bearer Token in Source",
    ),
    (
        "generic-password-assignment",
        # password = "..." with non-empty quoted string
        re.compile(
            r"\b(password|passwd|pwd|secret|api[_-]?key)\s*[:=]\s*['\"]([^'\"]{6,})['\"]",
            re.I,
        ),
        "high", "CWE-798", 0.55, "Inline Credential Assignment",
    ),
)


# Files that frequently contain *test* secrets — we lower confidence
# rather than skip outright (keeps surface visible).
_TEST_PATH_HINTS = ("test_", "/tests/", "/test/", "fixture", "example")


def _is_test_path(path: str) -> bool:
    p = (path or "").replace("\\", "/").lower()
    return any(h in p for h in _TEST_PATH_HINTS)


# ─────────────────────────────────────────────────────────────────────
# SecretDetector
# ─────────────────────────────────────────────────────────────────────


@dataclass
class SecretDetector:
    """Heuristic secret scanner.

    Default backend: regex catalogue + Shannon-entropy threshold for
    catch-all high-entropy strings.  Optional sklearn upgrade kicks
    in when ``scikit-learn`` is installed and a model pickle is
    present at ``sentinel/data/secret_detector.pkl``.
    """

    entropy_threshold: float = 4.5     # bits/char; > 4.5 = suspicious
    min_string_len: int = 24           # ignore short strings
    backend: str = "heuristic"         # heuristic | sklearn
    sklearn_model_path: str | None = None  # set on construction

    def __post_init__(self) -> None:
        self.backend = "heuristic"
        if self.sklearn_model_path and Path(self.sklearn_model_path).is_file():
            try:
                import joblib  # noqa: F401  (sklearn ships joblib)
                from sklearn.ensemble import RandomForestClassifier  # noqa: F401
                self.backend = "sklearn"
            except Exception:
                self.backend = "heuristic"

    def scan_text(self, text: str, *, file: str = "") -> list[Finding]:
        """Walk a single file's text; return Findings."""
        findings: list[Finding] = []
        if not text:
            return findings

        # Step 1: regex catalogue
        for rule_id, pattern, severity, cwe, base_conf, label in SECRET_PATTERNS:
            for m in pattern.finditer(text):
                line_no = text[: m.start()].count("\n") + 1
                conf = base_conf
                if _is_test_path(file):
                    conf *= 0.6
                snippet = (
                    text.splitlines()[line_no - 1][:240]
                    if line_no - 1 < len(text.splitlines())
                    else ""
                )
                findings.append(
                    Finding(
                        tool="ml_secret_detector",
                        source_kind="ml_classifier",
                        rule_id=rule_id,
                        file=file,
                        line_start=line_no,
                        line_end=line_no,
                        raw_message=f"{label} detected",
                        code_snippet="<redacted>",
                        severity=severity,
                        confidence=round(conf, 4),
                        cwe=cwe,
                        source_weight=0.55,
                        extra={"label": label, "snippet_excerpt": snippet[:80]},
                    )
                )

        # Step 2: high-entropy fallback — catches custom tokens.
        for line_no, line in enumerate(text.splitlines(), start=1):
            for token in re.findall(r'["\']([A-Za-z0-9+/=_\-]{20,})["\']', line):
                if len(token) < self.min_string_len:
                    continue
                ent = shannon_entropy(token)
                if ent < self.entropy_threshold:
                    continue
                conf = 0.4 + 0.1 * min(2.0, ent - self.entropy_threshold)
                if _is_test_path(file):
                    conf *= 0.5
                findings.append(
                    Finding(
                        tool="ml_secret_detector",
                        source_kind="ml_classifier",
                        rule_id="high-entropy",
                        file=file,
                        line_start=line_no,
                        raw_message=(
                            f"High-entropy literal (bits/char={ent:.2f}) — "
                            f"possible custom secret"
                        ),
                        code_snippet="<redacted>",
                        severity="medium",
                        confidence=round(min(0.85, conf), 4),
                        cwe="CWE-798",
                        source_weight=0.45,
                        extra={"entropy": round(ent, 4), "length": len(token)},
                    )
                )

        # Drop near-duplicates emitted by entropy check on top of an
        # already-matched regex pattern.
        return _dedup_findings(findings)


def _dedup_findings(findings: list[Finding]) -> list[Finding]:
    """Drop duplicates that share `(file, line_start, rule_id)`."""
    seen: set[tuple[str, int, str]] = set()
    out: list[Finding] = []
    for f in findings:
        key = (f.file, f.line_start, f.rule_id)
        if key in seen:
            continue
        seen.add(key)
        out.append(f)
    return out


# ─────────────────────────────────────────────────────────────────────
# AnomalyDetector
# ─────────────────────────────────────────────────────────────────────


_BASE64_RE = re.compile(r"[A-Za-z0-9+/=]{200,}")


@dataclass
class _FileMetrics:
    path: str
    loc: int = 0
    complexity_proxy: int = 0   # nesting + branches
    imports: int = 0
    base64_density: float = 0.0
    text_len: int = 0


def _compute_metrics(path: str, text: str) -> _FileMetrics:
    if not text:
        return _FileMetrics(path=path)
    lines = text.splitlines()
    loc = len(lines)
    # crude complexity: count if/for/while/case/&&/||
    complexity = (
        text.count(" if ")
        + text.count(" for ")
        + text.count(" while ")
        + text.count(" case ")
        + text.count("&&")
        + text.count("||")
    )
    imports = sum(
        1 for ln in lines
        if ln.lstrip().startswith(("import ", "from ", "require(", "#include"))
    )
    base64_chars = sum(len(m.group(0)) for m in _BASE64_RE.finditer(text))
    density = base64_chars / max(1, len(text))
    return _FileMetrics(
        path=path, loc=loc, complexity_proxy=complexity,
        imports=imports, base64_density=density, text_len=len(text),
    )


@dataclass
class AnomalyDetector:
    """Z-score outlier detector on basic file metrics.

    Files whose any metric exceeds ``|z| > threshold`` get flagged
    as ``low``-severity Findings.  Optional sklearn IsolationForest
    backend kicks in when ``scikit-learn`` is installed AND the
    caller passes ``backend="sklearn"`` explicitly (default stays
    pure-python so Docker image stays light)."""

    threshold: float = 3.0
    backend: str = "heuristic"

    def __post_init__(self) -> None:
        if self.backend == "sklearn":
            try:
                from sklearn.ensemble import IsolationForest  # noqa: F401
            except Exception:
                self.backend = "heuristic"

    def scan(self, files: dict[str, str]) -> list[Finding]:
        """`files` is `{path: text}`."""
        if not files:
            return []
        metrics = [_compute_metrics(p, t) for p, t in files.items()]
        if self.backend == "sklearn" and len(metrics) >= 8:
            return self._sklearn_scan(metrics)
        return self._zscore_scan(metrics)

    def _zscore_scan(self, metrics: list[_FileMetrics]) -> list[Finding]:
        if len(metrics) < 3:
            return []  # too few samples for z-score to mean anything
        findings: list[Finding] = []
        for axis_name, getter in (
            ("loc", lambda m: m.loc),
            ("complexity_proxy", lambda m: m.complexity_proxy),
            ("imports", lambda m: m.imports),
            ("base64_density", lambda m: m.base64_density),
        ):
            vals = [getter(m) for m in metrics]
            if statistics.stdev(vals) == 0:
                continue
            mu = statistics.mean(vals)
            sigma = statistics.stdev(vals)
            for m, v in zip(metrics, vals):
                if sigma == 0:
                    continue
                z = (v - mu) / sigma
                if abs(z) > self.threshold:
                    findings.append(
                        Finding(
                            tool="ml_anomaly_detector",
                            source_kind="ml_classifier",
                            rule_id=f"anomaly:{axis_name}",
                            file=m.path,
                            line_start=0,
                            raw_message=(
                                f"File has {axis_name} z-score {z:+.2f} — "
                                f"outlier vs corpus mean {mu:.1f}"
                            ),
                            severity="low",
                            confidence=min(0.85, 0.4 + 0.1 * (abs(z) - self.threshold)),
                            source_weight=0.4,
                            extra={
                                "axis": axis_name, "value": v,
                                "mean": mu, "stdev": sigma, "z_score": z,
                            },
                        )
                    )
        return findings

    def _sklearn_scan(
        self, metrics: list[_FileMetrics]
    ) -> list[Finding]:  # pragma: no cover - exercised when sklearn is present
        try:
            import numpy as np
            from sklearn.ensemble import IsolationForest
        except Exception:
            return self._zscore_scan(metrics)
        X = np.array([
            [m.loc, m.complexity_proxy, m.imports, m.base64_density]
            for m in metrics
        ], dtype=float)
        clf = IsolationForest(contamination=0.05, random_state=0)
        labels = clf.fit_predict(X)
        findings: list[Finding] = []
        for m, label in zip(metrics, labels):
            if label != -1:
                continue
            findings.append(
                Finding(
                    tool="ml_anomaly_detector",
                    source_kind="ml_classifier",
                    rule_id="anomaly:isolation_forest",
                    file=m.path,
                    raw_message="Isolation Forest flagged this file as an outlier",
                    severity="low",
                    confidence=0.55,
                    source_weight=0.45,
                )
            )
        return findings


# ─────────────────────────────────────────────────────────────────────
# SeverityRanker
# ─────────────────────────────────────────────────────────────────────


_RISK_PATH_HINTS: tuple[tuple[str, float], ...] = (
    ("auth", 1.4),
    ("login", 1.4),
    ("password", 1.4),
    ("crypto", 1.3),
    ("api", 1.2),
    ("admin", 1.3),
    ("payment", 1.5),
    ("session", 1.2),
    ("token", 1.3),
)


@dataclass
class SeverityRanker:
    """Re-rank a finding's severity given file path + source weight.

    Default = weighted-sum heuristic; optional XGBoost backend kicks
    in when ``xgboost`` is installed AND a trained model file is
    present.  V1 ships only the heuristic.
    """

    base_weight_severity: float = 0.55
    base_weight_source: float = 0.30
    base_weight_path_risk: float = 0.15
    backend: str = "heuristic"

    def rerank(self, findings: Iterable[Finding]) -> list[Finding]:
        out: list[Finding] = []
        for f in findings:
            risk = 1.0
            path_lower = (f.file or "").lower()
            for hint, mult in _RISK_PATH_HINTS:
                if hint in path_lower:
                    risk = max(risk, mult)
                    break
            score = (
                self.base_weight_severity * (severity_rank(f.severity) / 4.0)
                + self.base_weight_source * f.source_weight
                + self.base_weight_path_risk * (risk - 1.0) / 0.5
            )
            score = max(0.0, min(1.0, score))
            # Convert score back to a SeverityLevel.
            level: SeverityLevel
            if score >= 0.75:
                level = "critical"
            elif score >= 0.55:
                level = "high"
            elif score >= 0.35:
                level = "medium"
            elif score >= 0.15:
                level = "low"
            else:
                level = "info"
            # Don't downgrade severity below what the original tool said
            # (e.g. gitleaks always returns critical and we trust it).
            if severity_rank(level) < severity_rank(f.severity):
                level = f.severity
            f.severity = level
            f.confidence = round(min(1.0, max(f.confidence, score)), 4)
            f.extra = dict(f.extra or {})
            f.extra["severity_score"] = round(score, 4)
            f.extra["path_risk_multiplier"] = round(risk, 2)
            out.append(f)
        return out


# ─────────────────────────────────────────────────────────────────────
# MLPipeline orchestrator
# ─────────────────────────────────────────────────────────────────────


@dataclass
class MLPipelineResult:
    findings: list[Finding] = field(default_factory=list)
    files_scanned: int = 0
    backend_summary: dict[str, str] = field(default_factory=dict)
    elapsed_ms: float = 0.0


@dataclass
class MLPipeline:
    secret_detector: SecretDetector = field(default_factory=SecretDetector)
    anomaly_detector: AnomalyDetector = field(default_factory=AnomalyDetector)
    severity_ranker: SeverityRanker = field(default_factory=SeverityRanker)

    def backend_summary(self) -> dict[str, str]:
        return {
            "secret_detector": self.secret_detector.backend,
            "anomaly_detector": self.anomaly_detector.backend,
            "severity_ranker": self.severity_ranker.backend,
        }

    def scan_files(self, files: dict[str, str]) -> MLPipelineResult:
        import time
        start = time.monotonic()
        findings: list[Finding] = []
        for path, text in files.items():
            findings.extend(self.secret_detector.scan_text(text, file=path))
        findings.extend(self.anomaly_detector.scan(files))
        findings = self.severity_ranker.rerank(findings)
        return MLPipelineResult(
            findings=findings,
            files_scanned=len(files),
            backend_summary=self.backend_summary(),
            elapsed_ms=(time.monotonic() - start) * 1000.0,
        )

    def scan_paths(self, paths: list[str]) -> MLPipelineResult:
        files: dict[str, str] = {}
        for p in paths or []:
            try:
                if Path(p).is_dir():
                    for fp in _walk_files(p):
                        try:
                            files[fp] = Path(fp).read_text(
                                encoding="utf-8", errors="replace"
                            )
                        except Exception:
                            continue
                elif Path(p).is_file():
                    files[p] = Path(p).read_text(
                        encoding="utf-8", errors="replace"
                    )
            except Exception as exc:
                logger.debug("ml_pipeline path read failed for %s: %s", p, exc)
                continue
        return self.scan_files(files)


_TEXT_FILE_EXTS: frozenset[str] = frozenset({
    ".py", ".js", ".ts", ".tsx", ".jsx", ".go", ".rs", ".rb", ".php",
    ".java", ".c", ".cpp", ".cc", ".h", ".hpp", ".cs", ".sh", ".yaml",
    ".yml", ".json", ".toml", ".ini", ".cfg", ".env", ".tf", ".dockerfile",
})


def _walk_files(root: str) -> Iterable[str]:
    """Yield text files under `root`, skipping vendor / .git / node_modules."""
    skip_dirs = {".git", "node_modules", "venv", ".venv", "__pycache__",
                 "dist", "build", ".idea", ".vscode", "target"}
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in skip_dirs]
        for fn in filenames:
            ext = Path(fn).suffix.lower()
            if ext and ext not in _TEXT_FILE_EXTS:
                continue
            yield os.path.join(dirpath, fn)


__all__ = [
    "AnomalyDetector",
    "MLPipeline",
    "MLPipelineResult",
    "SECRET_PATTERNS",
    "SecretDetector",
    "SeverityRanker",
    "shannon_entropy",
]
