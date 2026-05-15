"""
Cycle D — Domain-aware feature templates.

The user's pain point: "ben ‘yılan oyunu’ desem, sistem onun
modern CSS+JS+canvas+kontroller+skor+restart vb. gerektireceğini
TAHMİN edip eklemeli."  In other words, the AI shouldn't ship a
literal-minimum implementation of a vague prompt — it should
proactively expand the request into the full production-grade
feature set the domain implies.

This module is the static knowledge base that drives that expansion.
For each domain (game / web app / CLI / REST API / library / data
processing) we list:

  * ``description`` — one-liner the planner injects so the model
    understands why these features are required
  * ``must_have_features`` — the non-negotiable list (e.g. game
    REQUIRES a game loop + collision detection + score + restart)
  * ``nice_to_have`` — features the planner mentions as "include if
    reasonable" so the model has guidance without forcing bloat
  * ``ground_rules`` — domain-specific quality rules (game: 60 fps
    target; api: input validation; cli: --help flag) injected into
    the coder prompt
  * ``preferred_languages`` — keys into LANGUAGE_RUNNERS the
    planner prefers when triage was ambiguous (game → html canvas
    over python+pygame because sandboxes are headless)

Detection is rule-based via ``_detect_domain`` — keyword patterns
matched against the user prompt.  When no template fires the
pipeline degrades gracefully to the baseline (planner-only) flow.
"""

from __future__ import annotations

import re
from typing import Any


# ─── Per-domain templates ─────────────────────────────────────────


DOMAIN_FEATURE_TEMPLATES: dict[str, dict[str, Any]] = {
    "game": {
        "description": (
            "Browser-runnable arcade game.  Must be playable end-to-"
            "end inside a single HTML file using HTML5 Canvas + plain "
            "JavaScript + CSS, with no external assets / no build step."
        ),
        "must_have_features": [
            "HTML5 <canvas> rendering (single canvas, full-window or fixed-size)",
            "requestAnimationFrame-driven game loop with delta-time",
            "Keyboard controls (arrow keys + WASD as fallback)",
            "Visible score counter in the UI",
            "Game over screen with final score",
            "Restart / play-again button (keyboard + click)",
            "Collision detection appropriate to the game (walls, self, foes)",
            "Modern dark-themed CSS (high-contrast, system-font stack)",
            "Responsive layout that adapts to window resize",
        ],
        "nice_to_have": [
            "Mobile touch / swipe controls for the canvas",
            "Smooth animations (eases, no janky frame-drops)",
            "Subtle sound effects via the Web Audio API (no external audio files)",
            "High-score tracking via localStorage",
            "Pause / resume support",
            "Difficulty ramp-up (speed, spawn rate, etc.)",
        ],
        "ground_rules": [
            "Single self-contained HTML file: <!DOCTYPE html> + <style> + <script>.",
            "No external script/style imports — sandboxes are network-isolated for HTML runs.",
            "Fixed-aspect canvas (e.g. 600x600) centred via flex/grid; never window.alert().",
            "Encapsulate state in a single ``game`` object; no globals scattered across functions.",
            "Game loop uses ``requestAnimationFrame`` with a ``last`` timestamp for delta-time.",
            "Restart button resets state without reloading the page.",
        ],
        "preferred_languages": ["html"],
    },
    "web_app": {
        "description": (
            "Single-page web application.  HTML structure + CSS "
            "styling + JavaScript interactivity, served as a "
            "self-contained file."
        ),
        "must_have_features": [
            "Semantic HTML structure (header / main / footer)",
            "Responsive CSS (works on phone + desktop)",
            "Modern dark-themed default styling",
            "Form validation + visible error messages",
            "Accessibility: alt text, ARIA labels, keyboard navigation",
        ],
        "nice_to_have": [
            "Light / dark mode toggle",
            "localStorage persistence for user state",
            "Fade / slide animations on state changes",
            "Empty / loading / error UI states",
        ],
        "ground_rules": [
            "Single self-contained HTML file.",
            "Mobile-first CSS — start with phone styles, expand with @media.",
            "Use CSS variables for theme tokens (--color-bg, --color-fg, etc.).",
            "All interactive elements reachable via Tab.",
        ],
        "preferred_languages": ["html"],
    },
    "cli_tool": {
        "description": (
            "Command-line tool.  Reads arguments from sys.argv "
            "(NOT input()) and prints structured output to stdout."
        ),
        "must_have_features": [
            "Argument parsing via the language's idiomatic library (argparse / clap / cobra / commander)",
            "--help flag listing every option with descriptions",
            "Exit codes: 0 = success, non-zero = specific failure",
            "Structured stdout (JSON or human-readable; pick one and document)",
            "Errors go to stderr; never to stdout",
            "Input validation with clear error messages (NOT stack traces)",
        ],
        "nice_to_have": [
            "Verbose / quiet flags for log volume",
            "Colour output when stdout is a TTY (auto-detected)",
            "Configuration file support (~/.toolrc or env var)",
        ],
        "ground_rules": [
            "Sandbox is non-interactive — NEVER use input() / readline / scanf.",
            "Demonstrate functionality from main() with sample argv (e.g. ['', '--help'] then a real call).",
            "Validate every argument before doing work; fail fast with helpful messages.",
        ],
    },
    "rest_api": {
        "description": (
            "HTTP REST API exposing CRUD-style endpoints.  Includes "
            "request validation, error handling, and tests."
        ),
        "must_have_features": [
            "Health check endpoint (GET /health → 200)",
            "RESTful resource routes (GET / POST / PUT / DELETE)",
            "Request body validation with clear 4xx errors",
            "JSON responses + correct Content-Type",
            "Error handler that catches uncaught exceptions → 500 with JSON body",
            "At least one round-trip test that exercises every endpoint",
        ],
        "nice_to_have": [
            "OpenAPI / Swagger documentation",
            "Pagination on list endpoints",
            "Auth middleware stub",
            "Request ID / structured logging",
        ],
        "ground_rules": [
            "No interactive prompts — server starts on a port and exits cleanly.",
            "Configurable port via env var or argv (default 8000).",
            "Use the language's idiomatic framework: FastAPI / Flask (Python), Express (Node), Gin (Go), Actix (Rust).",
            "Tests use the framework's TestClient; no external HTTP calls.",
        ],
    },
    "library": {
        "description": (
            "Reusable library / module.  Pure functions or a small "
            "class API.  Demonstrate via main() that calls the API "
            "with sample inputs."
        ),
        "must_have_features": [
            "Public API documented via docstrings / doc comments",
            "Type hints on every public signature",
            "main() that demonstrates each public function with sample inputs",
            "Comprehensive tests for happy path + edge cases + error cases",
            "Custom exception types for fallible operations (no bare ValueError)",
        ],
        "nice_to_have": [
            "Property-based test cases for invariants",
            "Performance microbenchmarks for hot functions",
            "README-style usage example in a leading docstring",
        ],
        "ground_rules": [
            "Pure-function preferred over OOP unless state is intrinsic.",
            "No I/O in library code — caller controls IO.",
            "Tests must run in CI (no network, no filesystem outside /tmp).",
        ],
    },
    "data_processing": {
        "description": (
            "Data ingest / transform / output pipeline.  Reads from a "
            "fixed source, transforms, writes structured output."
        ),
        "must_have_features": [
            "Fixed sample input (hard-coded or generated; NO interactive input)",
            "Transformation step with clear contract (input shape → output shape)",
            "Output as structured data (JSON / CSV / dataclass)",
            "Validation that input matches expected schema",
            "Tests for each transformation step",
        ],
        "nice_to_have": [
            "Streaming / chunked processing for large inputs",
            "Logging at INFO level for pipeline progress",
            "CSV / JSON / Parquet I/O helper",
        ],
        "ground_rules": [
            "Sandbox has no network — NEVER fetch from URLs at runtime.",
            "Generate sample input deterministically (random.seed) so tests are reproducible.",
        ],
    },
}


# ─── Detection ───────────────────────────────────────────────────


_GAME_KEYWORDS = (
    "snake game", "tetris", "pong", "breakout", "tic tac toe",
    "minesweeper", "2048", "flappy", "asteroids", "platformer",
    "arcade", "puzzle game", "shooter", "rpg", "side-scroller",
    "yılan oyun", "yilan oyun", "oyun yap",
)

_WEB_APP_KEYWORDS = (
    "landing page", "single page app", "spa", "web app", "website",
    "web sitesi", "static site", "homepage", "portfolio site",
    "marketing page", "blog template",
)

_CLI_KEYWORDS = (
    "cli ", "cli tool", "command line", "command-line",
    "argparse", "shell script", "bash script", "todo cli",
    "komut satırı", "konsol uygulaması",
)

_API_KEYWORDS = (
    "rest api", "restful api", "http api", "api server",
    "fastapi", "flask api", "express api", "endpoint",
    "graphql", "api endpoint",
)

_LIBRARY_KEYWORDS = (
    "library", "module", "package", "kütüphane",
    "public api", "function library", "utility library",
    # Common implicit-library asks: "fizzbuzz", "is_palindrome",
    # "fibonacci function", "calculator function".  Captured by
    # the per-language `function`/`func`/`fn` heuristic in
    # ``_detect_domain``.
)

_DATA_KEYWORDS = (
    "data processing", "etl", "csv parser", "json transform",
    "data pipeline", "veri işleme", "veri analizi",
    "scrape", "crawler",
)


def _detect_domain(prompt: str) -> dict[str, Any] | None:
    """Cycle D — rule-based domain inference.  Maps the user's prompt
    to one of ``DOMAIN_FEATURE_TEMPLATES`` keys based on keyword
    patterns.  Returns ``None`` when no domain matches strongly —
    the pipeline then falls back to the baseline planner-only flow.

    Detection is intentionally conservative: matching ``game`` because
    a prompt mentions "game" alone would be over-eager (could be
    "game theory" → math library).  We require a 2-keyword combo OR
    a strong canonical phrase like "snake game" / "rest api".

    Returns shape::

        {
            "domain": "game",
            "subdomain": "arcade",
            "description": "...",
            "must_have_features": [...],
            "nice_to_have": [...],
            "ground_rules": [...],
            "preferred_languages": [...],
        }
    """
    if not prompt:
        return None
    p = prompt.lower()

    # Game (highest priority — "snake game" must beat "fastapi tutorial")
    if any(kw in p for kw in _GAME_KEYWORDS):
        return {"domain": "game", "subdomain": "arcade",
                **DOMAIN_FEATURE_TEMPLATES["game"]}

    # REST API
    if any(kw in p for kw in _API_KEYWORDS):
        return {"domain": "rest_api", "subdomain": "http",
                **DOMAIN_FEATURE_TEMPLATES["rest_api"]}

    # CLI tool
    if any(kw in p for kw in _CLI_KEYWORDS):
        return {"domain": "cli_tool", "subdomain": "argv",
                **DOMAIN_FEATURE_TEMPLATES["cli_tool"]}

    # Web app (note: "website" must NOT collide with game's
    # "snake game website" — handled because game keywords ran
    # first)
    if any(kw in p for kw in _WEB_APP_KEYWORDS):
        return {"domain": "web_app", "subdomain": "spa",
                **DOMAIN_FEATURE_TEMPLATES["web_app"]}

    # Data processing
    if any(kw in p for kw in _DATA_KEYWORDS):
        return {"domain": "data_processing", "subdomain": "etl",
                **DOMAIN_FEATURE_TEMPLATES["data_processing"]}

    # Library — implicit when prompt mentions a "function" / "func" /
    # "fn" / "method" alongside a name.  "fizzbuzz function in python"
    # → library.  "fibonacci function" → library.
    if (
        any(kw in p for kw in _LIBRARY_KEYWORDS)
        or re.search(r"\b(?:function|func|fn|method)\b", p)
        or re.search(r"\b(?:fizzbuzz|fibonacci|palindrome|primes?|factorial)\b", p)
    ):
        return {"domain": "library", "subdomain": "function",
                **DOMAIN_FEATURE_TEMPLATES["library"]}

    return None


def render_domain_directive(detection: dict[str, Any]) -> str:
    """Render a planner-prompt-friendly directive listing the domain's
    must-have features.  The planner is told to cover EVERY one in
    its plan (so the coder receives them as concrete spec items)."""
    if not detection:
        return ""
    must = "\n".join(f"  • {f}" for f in detection.get("must_have_features", []))
    nice = "\n".join(f"  • {f}" for f in detection.get("nice_to_have", []))
    rules = "\n".join(f"  • {r}" for r in detection.get("ground_rules", []))
    return (
        f"\n\nDETECTED DOMAIN: {detection['domain']} / {detection['subdomain']}\n"
        f"{detection.get('description', '')}\n\n"
        "MUST-HAVE features (your plan MUST include EVERY one of these "
        "as a concrete step; the coder will be evaluated on coverage):\n"
        f"{must}\n\n"
        "NICE-TO-HAVE features (include when they fit the request size; "
        "skip when they'd bloat a 100-line deliverable):\n"
        f"{nice}\n\n"
        "DOMAIN-SPECIFIC GROUND RULES (always apply):\n"
        f"{rules}\n"
    )


def render_coder_directive(detection: dict[str, Any]) -> str:
    """Render a coder-prompt-friendly directive — same domain spec
    but framed as production-quality requirements the implementation
    must satisfy."""
    if not detection:
        return ""
    must = "\n".join(f"  ✓ {f}" for f in detection.get("must_have_features", []))
    rules = "\n".join(f"  • {r}" for r in detection.get("ground_rules", []))
    return (
        f"\n\nPRODUCTION-QUALITY REQUIREMENTS for "
        f"{detection['domain']}/{detection['subdomain']}:\n"
        "Your implementation MUST include each of these:\n"
        f"{must}\n\n"
        "Domain-specific rules (non-negotiable):\n"
        f"{rules}\n"
    )


def feature_coverage(code: str, detection: dict[str, Any]) -> dict[str, Any]:
    """Cycle D — rough heuristic coverage check.  For each must-have
    feature, look for keyword fingerprints in the rendered code.
    Returns ``{covered: int, total: int, missing: [str], ratio: float}``.

    Used by the Reflexion loop to decide "does this code actually
    deliver what the domain requires?" — independent of the critic's
    score (which the user explicitly said NOT to over-rely on).
    """
    if not detection or not code:
        return {"covered": 0, "total": 0, "missing": [], "ratio": 1.0}

    code_l = code.lower()
    must = detection.get("must_have_features", [])
    if not must:
        return {"covered": 0, "total": 0, "missing": [], "ratio": 1.0}

    # Per-feature fingerprint patterns.  Each entry maps a feature
    # description prefix to a set of strings that strongly indicate
    # the feature is implemented.  Conservative: all-or-nothing
    # match on ANY fingerprint counts as covered.
    fingerprints: dict[str, tuple[str, ...]] = {
        "html5 <canvas>": ("<canvas", "getcontext"),
        "requestanimationframe": ("requestanimationframe",),
        "keyboard controls": ("keydown", "addeventlistener"),
        "visible score": ("score", "innertext"),
        "game over screen": ("game over", "gameover"),
        "restart": ("restart", "play again", "again"),
        "collision detection": ("collision", "intersect", "overlap"),
        "modern dark-themed": ("background", "color"),
        "responsive layout": ("@media", "resize"),
        "mobile touch": ("touchstart", "touchmove", "swipe"),
        "smooth animations": ("transition", "animation", "ease"),
        "sound effects": ("audiocontext", "oscillator"),
        "high-score": ("localstorage",),
        "pause / resume": ("pause", "resume"),
        "difficulty ramp-up": ("speed", "interval"),
        # Generic / cross-domain
        "semantic html": ("<header", "<main", "<footer"),
        "form validation": ("required", "validity"),
        "accessibility": ("aria-", "alt="),
        "argument parsing": ("argparse", "argv", "clap", "cobra"),
        "--help flag": ("--help", "help "),
        "exit codes": ("sys.exit", "exit("),
        "structured stdout": ("json.dumps", "print(json"),
        "errors go to stderr": ("stderr",),
        "input validation": ("validate", "valid", "raise"),
        "health check": ("/health",),
        "restful resource": ("get(", "post(", "put(", "delete("),
        "request body validation": ("schema", "validate"),
        "json responses": ("application/json", "tojson", "json("),
        "error handler": ("exception", "errorhandler", "@app.errorhandler"),
        "round-trip test": ("client.get", "client.post", "test_"),
        "public api documented": ('"""', "/**"),
        "type hints": ("->", ": "),
        "main() that demonstrates": ("def main", "fn main", "func main"),
        "comprehensive tests": ("def test_", "func test"),
        "custom exception types": ("class.*error", "raise.*error"),
        "fixed sample input": ("=", "data"),
        "transformation step": ("def ", "fn ", "func "),
        "output as structured": ("json", "csv", "dataclass"),
        "validation that input": ("validate", "schema"),
        "tests for each transformation": ("def test_", "assert"),
    }

    missing: list[str] = []
    covered = 0
    for feature in must:
        feature_l = feature.lower()
        # Find the matching fingerprint by prefix
        fps: tuple[str, ...] = ()
        for prefix, candidates in fingerprints.items():
            if prefix in feature_l:
                fps = candidates
                break
        if not fps:
            # No fingerprint defined → assume covered (don't penalise
            # bespoke features the heuristic doesn't know about)
            covered += 1
            continue
        if any(fp in code_l for fp in fps):
            covered += 1
        else:
            missing.append(feature)

    total = len(must)
    return {
        "covered": covered,
        "total": total,
        "missing": missing,
        "ratio": covered / total if total > 0 else 1.0,
    }
