"""
Cycle D — Domain-aware production-quality tests.

Drives the user request: "ben ‘yılan oyunu’ desem dahi onu en
gelişmiş css'lerin js kodlarının ekleneceğini tahmin edebilmeli".
The pipeline MUST automatically expand a vague request into a
domain-appropriate, production-grade feature set.
"""

from __future__ import annotations

import pytest

from document_processor.code_intelligence.domain_templates import (
    DOMAIN_FEATURE_TEMPLATES,
    _detect_domain,
    feature_coverage,
    render_coder_directive,
    render_domain_directive,
)


# ─── Domain detection ────────────────────────────────────────────


class TestDomainDetection:
    @pytest.mark.parametrize("prompt,expected_domain", [
        ("snake game", "game"),
        ("yılan oyunu yap", "game"),
        ("oyun yap arkadaşım", "game"),
        ("tetris clone", "game"),
        ("flappy bird game", "game"),
        ("a rest api for users", "rest_api"),
        ("flask api with three endpoints", "rest_api"),
        ("a cli tool for json formatting", "cli_tool"),
        ("a command-line todo manager", "cli_tool"),
        ("a landing page for my startup", "web_app"),
        ("a website with dark mode", "web_app"),
        ("a fizzbuzz function in python", "library"),
        ("python is_palindrome function", "library"),
        ("fibonacci function", "library"),
        ("an etl pipeline for csv to json", "data_processing"),
    ])
    def test_detection_recognises_canonical_phrases(
        self, prompt, expected_domain,
    ):
        det = _detect_domain(prompt)
        assert det is not None, f"No domain detected for {prompt!r}"
        assert det["domain"] == expected_domain

    def test_returns_none_for_truly_ambiguous_prompts(self):
        # "explain machine learning" — pure expository, no domain cue.
        assert _detect_domain("explain machine learning") is None
        assert _detect_domain("how do databases work") is None
        # Empty prompt → None.
        assert _detect_domain("") is None
        assert _detect_domain(None) is None  # type: ignore[arg-type]

    def test_game_template_has_canvas_and_restart(self):
        det = _detect_domain("snake game")
        assert det is not None
        features = det["must_have_features"]
        assert any("canvas" in f.lower() for f in features)
        assert any("restart" in f.lower() for f in features)
        assert any("score" in f.lower() for f in features)
        assert any("game over" in f.lower() for f in features)

    def test_game_prefers_html(self):
        det = _detect_domain("snake game")
        assert det["preferred_languages"] == ["html"]

    def test_rest_api_template_includes_health_check(self):
        det = _detect_domain("a rest api for users")
        assert any("/health" in f.lower() or "health check" in f.lower()
                   for f in det["must_have_features"])

    def test_library_template_includes_main_demo(self):
        det = _detect_domain("python fizzbuzz function")
        assert any("main()" in f or "demonstrates" in f.lower()
                   for f in det["must_have_features"])


# ─── Render directives ──────────────────────────────────────────


class TestRenderDirectives:
    def test_planner_directive_lists_must_haves(self):
        det = _detect_domain("snake game")
        directive = render_domain_directive(det)
        # Every must-have appears
        for feature in det["must_have_features"]:
            assert feature in directive
        # Says it's a directive the planner MUST cover
        assert "MUST include EVERY one" in directive

    def test_coder_directive_lists_must_haves_with_checkmarks(self):
        det = _detect_domain("snake game")
        directive = render_coder_directive(det)
        assert "PRODUCTION-QUALITY REQUIREMENTS" in directive
        # ✓ markers
        assert "✓" in directive

    def test_no_directive_for_none_detection(self):
        assert render_domain_directive(None) == ""  # type: ignore[arg-type]
        assert render_coder_directive(None) == ""  # type: ignore[arg-type]


# ─── Feature coverage ────────────────────────────────────────────


class TestFeatureCoverage:
    def test_full_coverage_when_code_has_every_fingerprint(self):
        det = _detect_domain("snake game")
        # Code that contains every fingerprint we care about
        code = """
        <!DOCTYPE html>
        <canvas id="game"></canvas>
        <script>
            const ctx = canvas.getContext("2d");
            requestAnimationFrame(loop);
            document.addEventListener("keydown", handler);
            const score = document.getElementById("score");
            // game over when collision
            // restart button binding
            const audioContext = new AudioContext();
            localStorage.setItem("hi", "1");
            document.body.addEventListener("touchstart", swipe);
            // pause / resume
            // speed interval ramp-up
        </script>
        <style>
            body { background: #000; color: #fff; }
            @media (max-width: 600px) { body { font-size: 14px; } }
            .canvas { transition: transform 0.2s ease; }
        </style>
        """
        cov = feature_coverage(code, det)
        # We expect a high coverage ratio given the comprehensive code
        assert cov["ratio"] >= 0.7

    def test_low_coverage_for_minimal_implementation(self):
        det = _detect_domain("snake game")
        # Bare-bones snake game — missing most production features
        code = """
        # snake game in python
        snake = [(5, 5)]
        def move(): snake.append((snake[-1][0]+1, snake[-1][1]))
        move()
        print(snake)
        """
        cov = feature_coverage(code, det)
        assert cov["ratio"] < 0.5
        assert len(cov["missing"]) >= 5  # plenty of missing features

    def test_empty_code_returns_neutral(self):
        det = _detect_domain("snake game")
        cov = feature_coverage("", det)
        assert cov["ratio"] == 1.0  # neutral (no penalty for no code)

    def test_no_domain_returns_neutral(self):
        cov = feature_coverage("any code", None)  # type: ignore[arg-type]
        assert cov["ratio"] == 1.0


# ─── Templates structure invariants ─────────────────────────────


class TestTemplatesStructure:
    def test_every_template_has_required_keys(self):
        required = {
            "description", "must_have_features",
            "nice_to_have", "ground_rules",
        }
        for name, tmpl in DOMAIN_FEATURE_TEMPLATES.items():
            missing = required - set(tmpl.keys())
            assert not missing, f"{name}: missing keys {missing}"

    def test_must_haves_are_non_empty_strings(self):
        for name, tmpl in DOMAIN_FEATURE_TEMPLATES.items():
            assert tmpl["must_have_features"], f"{name}: empty must-haves"
            for f in tmpl["must_have_features"]:
                assert isinstance(f, str) and f.strip()
