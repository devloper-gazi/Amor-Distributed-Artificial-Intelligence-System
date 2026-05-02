"""Unit tests for ``document_processor/sentinel/ml_pipeline.py``."""

from __future__ import annotations

import pytest

from document_processor.sentinel.ml_pipeline import (
    AnomalyDetector,
    MLPipeline,
    SECRET_PATTERNS,
    SecretDetector,
    SeverityRanker,
    shannon_entropy,
)
from document_processor.sentinel.models import Finding


# ─── Shannon entropy ────────────────────────────────────────────────


def test_entropy_constant_string_zero():
    assert shannon_entropy("aaaaaaaaaa") == 0.0


def test_entropy_random_high():
    # 64-char base64-ish string should produce high entropy.
    s = "X9k_7Lp2qR4nT8vB3mZ5fC6dW1jY0eU"
    assert shannon_entropy(s) > 4.0


def test_entropy_empty_string_zero():
    assert shannon_entropy("") == 0.0


# ─── SecretDetector — regex catalogue ───────────────────────────────


def test_detect_aws_access_key():
    code = 'AWS_KEY = "AKIAIOSFODNN7EXAMPLE"'
    det = SecretDetector()
    out = det.scan_text(code, file="config.py")
    assert any(f.rule_id == "aws-access-key" for f in out)
    assert any(f.severity == "critical" for f in out)


def test_detect_github_pat():
    code = 'token = "ghp_AAAABBBBCCCCDDDDEEEEFFFF1234567890ABCDEF"'
    det = SecretDetector()
    out = det.scan_text(code, file="x.py")
    assert any(f.rule_id == "github-pat" for f in out)


def test_detect_private_key():
    code = "-----BEGIN OPENSSH PRIVATE KEY-----\ndata\n-----END\n"
    det = SecretDetector()
    out = det.scan_text(code, file="id_rsa")
    assert any(f.rule_id == "private-rsa-key" for f in out)


def test_detect_anthropic_key():
    code = 'API = "sk-ant-api03-' + "a" * 80 + '"'
    det = SecretDetector()
    out = det.scan_text(code, file="secrets.py")
    assert any(f.rule_id == "anthropic-api-key" for f in out)


def test_test_path_lowers_confidence():
    code = 'AWS = "AKIAIOSFODNN7EXAMPLE"'
    det = SecretDetector()
    prod = det.scan_text(code, file="src/config.py")
    test = det.scan_text(code, file="tests/test_config.py")
    prod_match = next(f for f in prod if f.rule_id == "aws-access-key")
    test_match = next(f for f in test if f.rule_id == "aws-access-key")
    # Test paths get a confidence multiplier so the test version
    # ends up below the prod version.
    assert test_match.confidence < prod_match.confidence


def test_high_entropy_fallback_catches_custom_token():
    # Random-looking 32-char token, not in the regex catalogue.
    high_ent = '"7XK9pR3qLm2nVtY8wZ4cF6sH1bJ0aE5d"'
    det = SecretDetector(entropy_threshold=4.0)
    out = det.scan_text(f"const token = {high_ent};\n", file="app.js")
    assert any(f.rule_id == "high-entropy" for f in out)


def test_secret_detector_skips_short_strings():
    # Below min_string_len → no entropy match.
    code = 'x = "short"'
    det = SecretDetector(min_string_len=100, entropy_threshold=4.0)
    out = det.scan_text(code, file="x.py")
    # Only regex-based hits could appear; "short" isn't in catalogue.
    assert all(f.rule_id != "high-entropy" for f in out)


# ─── AnomalyDetector ────────────────────────────────────────────────


def test_anomaly_detector_flags_outlier_loc():
    files = {
        "a.py": "x = 1\n" * 50,    # ~50 LOC
        "b.py": "x = 1\n" * 60,    # ~60 LOC
        "c.py": "x = 1\n" * 55,    # ~55 LOC
        "d.py": "x = 1\n" * 52,    # ~52 LOC
        "huge.py": "x = 1\n" * 5000,  # 100x outlier
    }
    det = AnomalyDetector(threshold=1.5)
    out = det.scan(files)
    flagged = [f.file for f in out if "loc" in f.rule_id]
    assert "huge.py" in flagged


def test_anomaly_detector_few_samples_no_flag():
    """< 3 files = sample too small for z-score; engine just skips."""
    det = AnomalyDetector()
    out = det.scan({"a.py": "x = 1"})
    assert out == []


def test_anomaly_detector_uniform_no_flag():
    files = {f"f{i}.py": "x = 1\n" * 50 for i in range(5)}
    det = AnomalyDetector(threshold=2.0)
    out = det.scan(files)
    # No outlier when all files are identical.
    assert out == []


# ─── SeverityRanker ─────────────────────────────────────────────────


def test_severity_ranker_path_risk_boosts_auth_paths():
    f = Finding(
        tool="bandit",
        file="src/auth/login.py",
        severity="medium",
        confidence=0.5,
        source_weight=0.6,
    )
    ranked = SeverityRanker().rerank([f])
    assert ranked[0].extra["path_risk_multiplier"] > 1.0
    # Same severity at minimum (we never downgrade below tool's claim)
    assert ranked[0].severity in ("medium", "high", "critical")


def test_severity_ranker_does_not_downgrade():
    f = Finding(tool="bandit", severity="critical", confidence=0.4, source_weight=0.2)
    ranked = SeverityRanker().rerank([f])
    assert ranked[0].severity == "critical"


def test_severity_ranker_neutral_path_no_boost():
    f = Finding(tool="bandit", file="src/utils.py", severity="low",
                confidence=0.4, source_weight=0.45)
    ranked = SeverityRanker().rerank([f])
    assert ranked[0].extra["path_risk_multiplier"] == 1.0


# ─── MLPipeline integration ─────────────────────────────────────────


def test_ml_pipeline_scan_files_combines_stages():
    files = {
        "src/auth.py": (
            'API_KEY = "ghp_AAAABBBBCCCCDDDDEEEEFFFF1234567890ABCDEF"\n'
            'def login(): pass\n'
        ),
        "tests/test_auth.py": "x = 1\n",
        "src/big.py": "x = 1\n" * 5000,
    }
    pipe = MLPipeline()
    res = pipe.scan_files(files)
    # Secret detector should have caught the GitHub PAT.
    assert any(f.rule_id == "github-pat" for f in res.findings)
    assert res.files_scanned == 3
    assert res.backend_summary["secret_detector"] in ("heuristic", "sklearn")


def test_ml_pipeline_backend_summary_logs_heuristic():
    pipe = MLPipeline()
    summary = pipe.backend_summary()
    assert summary["secret_detector"] == "heuristic"


# ─── Pattern catalogue smoke ────────────────────────────────────────


def test_secret_patterns_compile():
    """Every pattern compiles + has the expected tuple shape."""
    for entry in SECRET_PATTERNS:
        rule_id, pattern, sev, cwe, conf, label = entry
        assert isinstance(rule_id, str) and rule_id
        assert pattern.pattern  # regex compiled
        assert sev in ("info", "low", "medium", "high", "critical")
        assert cwe.startswith("CWE-")
        assert 0.0 <= conf <= 1.0
        assert label
