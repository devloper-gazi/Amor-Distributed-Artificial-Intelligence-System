# Code Intelligence Mode — Changelog

User-visible changes per commit. Newest first.

## v2 — feat/code-intelligence-mode-v2 branch

- **feat(code): infra wiring for v2 — deps + env + compose** —
  `requirements.txt` gets `PyYAML`, `networkx`, `huggingface_hub`,
  `PyGithub`, `arxiv` (all lazy + fail-open). `.env.example` and
  `docker-compose.yml` get the six new `CODE_CAPABILITY_*` and
  `CODE_LANGFUSE_*` env vars.
- **feat(ui): code-view handles adversarial_alert event + CSS** —
  Banner with severity pill + per-rule rows. Critical alerts mark the
  card `failed`.
- **feat(code): wire v2 modules — adversarial filter, /capabilities,
  lifespan** — `_publish` runs every event through `AdversarialReviewer`
  first. New endpoints: `GET /api/code/capabilities`,
  `POST /api/code/capabilities/discover`. `main.py` lifespan spawns
  `CapabilityDiscoverer.run_forever()` as a long-lived task.
- **feat(code): CapabilityDiscoverer — autonomous self-extension protocol**
  — Long-lived task that harvests candidates from HF/GitHub/arXiv,
  runs the six-gate pipeline (license → metadata → sandbox install →
  smoke → benchmark → registration), and writes passing entries to
  MongoDB collection `capabilities`. 18 tests covering both halves of
  the gate logic + the discovery loop.
- **feat(code): RepoMap — tree-sitter + PageRank workspace summary** —
  Aider-style workspace map with personalised PageRank, binary-search
  fitter to a token budget. tree-sitter / NetworkX missing → graceful
  regex + heuristic-rank fallback. 8 tests.
- **feat(code): AdversarialReviewer — synchronous event filter** —
  YAML rule pack covering prompt-injection, secret leakage, suspicious
  shell forms. Critical hits suppress the original event + flag the
  session for cancellation. 11 tests.
- **feat(code): observability — @traced decorator + Langfuse/JSONL
  fallback** — Wraps any async function with span emission. Langfuse
  when configured, JSONL otherwise. Failure-quiet. 5 tests.
- **docs(code): pre-flight inventory for Code Intelligence v2** —
  PRE_FLIGHT.md, PATTERNS.md, INVARIANTS.md, INTEGRATION_MAP.md.

## v1 — main branch (commit d4f48c8)

- **feat(code): Code Intelligence Mode — multi-agent local-only code
  engine** — Initial release. 11 backend Python files, 1 frontend JS
  module, 528-line CSS block, 9 routes, 5 specialist agents, 9-phase
  pipeline, Docker sandbox, static analysis, model registry. Verified
  zero external API calls.
