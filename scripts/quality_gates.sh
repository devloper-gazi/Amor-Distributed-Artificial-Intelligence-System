#!/usr/bin/env bash
# Charter §5 quality gate runner — one command, seven gates.
#
# Each gate runs in sequence; the script accumulates a non-zero exit
# code on first failure but continues so the operator gets a full
# picture in one pass.
#
# Scope: document_processor/code_intelligence/* + tests/code_intelligence/*
# + web_ui/static/js/code-view.js. The wider AMOR codebase isn't yet in
# scope (separate cleanup task) — see pyproject.toml extend-exclude.
#
# Usage:
#   bash scripts/quality_gates.sh           # run all gates
#   bash scripts/quality_gates.sh --quick   # skip pyright + coverage (CI smoke)

set -uo pipefail

BLUE='\033[0;34m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; RED='\033[0;31m'; NC='\033[0m'

QUICK=0
[[ "${1:-}" == "--quick" ]] && QUICK=1

EXIT_CODE=0
gate_count=0
gate_passed=0
gate_failed=0

run_gate() {
    local name=$1; shift
    gate_count=$((gate_count + 1))
    echo -e "${BLUE}── Gate ${gate_count}: ${name} ──${NC}"
    if "$@"; then
        echo -e "${GREEN}  ✓ ${name}${NC}"
        gate_passed=$((gate_passed + 1))
    else
        echo -e "${RED}  ✗ ${name} FAILED${NC}"
        gate_failed=$((gate_failed + 1))
        EXIT_CODE=1
    fi
}

skip_gate() {
    local name=$1; local reason=$2
    gate_count=$((gate_count + 1))
    echo -e "${YELLOW}── Gate ${gate_count}: ${name} — SKIPPED (${reason}) ──${NC}"
}

# ── Gate 1: Format ──────────────────────────────────────────────────
gate_1_format() {
    if ! command -v ruff >/dev/null 2>&1; then
        echo "    ruff not installed; install with: pip install ruff"
        return 1
    fi
    ruff format --check \
        document_processor/code_intelligence \
        tests/code_intelligence
}
run_gate "ruff format --check" gate_1_format

# ── Gate 2: Lint ────────────────────────────────────────────────────
gate_2_lint() {
    if ! command -v ruff >/dev/null 2>&1; then
        echo "    ruff not installed; install with: pip install ruff"
        return 1
    fi
    ruff check \
        document_processor/code_intelligence \
        tests/code_intelligence
}
run_gate "ruff check" gate_2_lint

# ── Gate 3: Type ────────────────────────────────────────────────────
gate_3_type() {
    if command -v pyright >/dev/null 2>&1; then
        pyright document_processor/code_intelligence
    elif python -c "import mypy" 2>/dev/null; then
        # Charter starts at basic mode; strict is a future cleanup.
        python -m mypy --ignore-missing-imports \
            document_processor/code_intelligence
    else
        echo "    neither pyright nor mypy installed"
        return 1
    fi
}
if [[ $QUICK -eq 0 ]]; then
    run_gate "pyright/mypy" gate_3_type
else
    skip_gate "pyright/mypy" "--quick"
fi

# ── Gate 4: Test + coverage ─────────────────────────────────────────
gate_4_test() {
    if ! python -c "import pytest" 2>/dev/null; then
        echo "    pytest not installed"
        return 1
    fi
    if [[ $QUICK -eq 1 ]]; then
        python -m pytest -q tests/code_intelligence
    else
        python -m pytest -q tests/code_intelligence \
            --cov=document_processor.code_intelligence \
            --cov-report=term-missing \
            --cov-fail-under=85
    fi
}
run_gate "pytest + coverage ≥ 85%" gate_4_test

# ── Gate 5: Security grep — zero paid-AI imports ────────────────────
gate_5_security_grep() {
    local pat='(anthropic_client|api\.openai\.com|api\.anthropic\.com|api\.cohere\.com|api\.voyageai\.com)'
    local hits
    hits=$(grep -rEn "$pat" \
        document_processor/code_intelligence \
        web_ui/static/js/code-view.js 2>/dev/null \
        | grep -v -i "forbid\|never\|do not\|don't\|skip" || true)
    if [[ -n "$hits" ]]; then
        echo "    paid-AI imports found:"
        echo "$hits" | sed 's/^/      /'
        return 1
    fi
    return 0
}
run_gate "zero paid-AI imports" gate_5_security_grep

# ── Gate 6: License sweep ───────────────────────────────────────────
gate_6_license() {
    if ! command -v pip-licenses >/dev/null 2>&1; then
        echo "    pip-licenses not installed; install with: pip install pip-licenses"
        return 1
    fi
    # Allow LGPL because PyGithub is LGPL — see LICENSE_NOTES.md.
    pip-licenses --fail-on='GPL;AGPL;SSPL' --packages \
        $(awk -F'[<>=]' '/^[a-zA-Z]/ {print $1}' requirements.txt | sort -u | tr '\n' ' ')
}
if [[ $QUICK -eq 0 ]]; then
    run_gate "pip-licenses" gate_6_license
else
    skip_gate "pip-licenses" "--quick"
fi

# ── Gate 7: Import boundary — no api/ or thinking/ imports from CI ──
gate_7_import_boundary() {
    local hits
    hits=$(grep -rEn "from document_processor\.(api|thinking)" \
        document_processor/code_intelligence 2>/dev/null \
        | grep -v "import call_ollama" || true)
    if [[ -n "$hits" ]]; then
        echo "    forbidden inbound imports:"
        echo "$hits" | sed 's/^/      /'
        return 1
    fi
    return 0
}
run_gate "import boundary" gate_7_import_boundary

# ── Summary ─────────────────────────────────────────────────────────
echo
if [[ $EXIT_CODE -eq 0 ]]; then
    echo -e "${GREEN}All ${gate_count} gates passed.${NC}"
else
    echo -e "${RED}${gate_failed}/${gate_count} gates FAILED${NC} (${gate_passed}/${gate_count} passed)"
fi
exit $EXIT_CODE
