# AMOR — convenience targets that wrap `python -m tools.setup`.
#
# All targets are idempotent.  Pick the Python interpreter via the
# `PY` variable (defaults to python3):
#
#     make install
#     make install PROFILE=dev
#     make install PY=python
#     make start
#     make stop
#     make doctor
#     make verify
#     make logs SVC=app

PY      ?= python3
SETUP   := $(PY) -m tools.setup
PROFILE ?= full
SVC     ?=

.PHONY: help install install-minimal install-baseline start stop restart \
        destroy status logs doctor verify preflight test clean \
        sprint0 sprint0-phi4

help:
	@echo "AMOR setup targets:"
	@echo ""
	@echo "  install            — full bootstrap (preflight + up + verify)"
	@echo "  install-minimal    — core data plane only (no GPU/Ollama)"
	@echo "  install-baseline   — full + auto-pull Mistral + Phi-4 judge GGUFs"
	@echo ""
	@echo "  start              — bring services up + wait-for-health"
	@echo "  stop               — stop containers (keeps volumes)"
	@echo "  restart            — restart + re-check health"
	@echo "  destroy            — compose down (use ARGS=--volumes to nuke data)"
	@echo "  status             — compose ps + health snapshot"
	@echo "  logs SVC=app       — tail logs (omit SVC for all)"
	@echo ""
	@echo "  doctor             — full read-only diagnostic"
	@echo "  verify             — live smoke probes"
	@echo "  preflight          — check host before install"
	@echo ""
	@echo "  test               — run setup unit tests"
	@echo ""
	@echo "  sprint0            — kick off the v18 Sprint 0 baseline (Mistral)"
	@echo "  sprint0-phi4       — same, with Phi-4 judge"

install:
	$(SETUP) install --profile $(PROFILE)

install-minimal:
	$(SETUP) install --profile minimal

install-baseline:
	$(SETUP) install --profile baseline

start:
	$(SETUP) start $(SVC)

stop:
	$(SETUP) stop $(SVC)

restart:
	$(SETUP) restart $(SVC)

destroy:
	$(SETUP) destroy $(ARGS)

status:
	$(SETUP) status

logs:
	$(SETUP) logs $(SVC)

doctor:
	$(SETUP) doctor

verify:
	$(SETUP) verify

preflight:
	$(SETUP) preflight

test:
	$(PY) -m pytest tests/setup -v

clean:
	@find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
	@find . -type f -name "*.pyc" -delete 2>/dev/null || true

sprint0:
	@bash tools/run_sprint0_v18.sh

sprint0-phi4:
	@AMOR_SPRINT0_JUDGE=phi4 bash tools/run_sprint0_v18.sh
