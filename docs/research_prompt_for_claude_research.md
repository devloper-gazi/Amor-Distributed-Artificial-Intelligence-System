# AMOR Code Intelligence — Frontier Research Brief (2026)

> Aşağıdaki prompt'u Claude Research'e (claude.ai → Research) doğrudan
> yapıştır.  Tek seferde derinlemesine multi-source research yapacak;
> her bölümün cevabını gerçek 2025-2026 makalelerine + benchmark'lara
> bağlayacak şekilde tasarlandı.

---

## 🎯 Master Research Prompt (kopyala-yapıştır)

I'm building **AMOR**, a local-first multi-agent code-generation system,
and I need a frontier-quality research dossier to plan its next major
upgrade. The constraint is real: I run everything on a single
**RTX 4060 with 8 GB VRAM** + **32 GB system RAM**, **llama.cpp +
llama-swap** as the inference layer, no cloud API. The current
pipeline is solid but **shallow**: it produces working code but lacks
the reasoning depth, tool orchestration, and production-quality
intuition that frontier coding systems show in 2026.

I want a comprehensive research output that covers six tracks. For
each track, I need:

1. **The 3-5 most important 2025-2026 papers / engineering writeups
   / open-source projects** with direct citations (arXiv IDs, GitHub
   repo links, blog URLs).
2. **One-paragraph distilled insight** per source — what makes it
   work, what's the contribution, what's the catch.
3. **Concrete recommendation for AMOR**: how would I integrate this
   given my hardware?
4. **Anti-patterns / dead-ends to avoid** — things that look promising
   in 2024 but didn't pan out by 2026.

Be ruthlessly specific. Avoid generalities. If a paper claims a
benchmark number, give me the number. If a project requires hardware
I don't have, say so explicitly.

---

### TRACK 1 — Multi-Agent Code Generation Architectures (2025-2026 frontier)

**Question:** What are the SOTA multi-agent code-gen architectures
that materially improve on plain "planner → coder → tester → debugger"
pipelines like mine?

Specifically investigate:
- **AlphaCodium** (CodiumAI, Ridnik et al. 2024) updates and
  successors — what's the current state of test-driven iterative
  flows? Did the original numbers (44% → 54% on CodeContests with
  GPT-4) hold up with smaller models?
- **Agentless / SWE-Agent / OpenHands SDK V1** (2024-2026) — which
  one wins on SWE-bench Verified by 2026? How do their reasoning
  budgets compare?
- **Reflexion vs Self-Refine vs Self-Debug** — empirical comparison
  on code tasks circa 2026. Which one survived?
- **CodeR / RepoCoder / Aider Architect** — production-grade
  multi-file, repo-aware patterns. Aider's "architect mode" results
  on real GitHub issues.
- **Self-Consistency + best-of-N for code** — does it beat single-pass
  with 2026 models? Cost/quality tradeoff.
- **Specification-first generation** — LMQL, Outlines, structured
  output schemas; does forcing a JSON spec before code improve quality?
- **2026 skill / sub-agent patterns** (Anthropic Agent Skills, Devin's
  skills library, Cursor Composer) — how should a multi-agent system
  decompose work?

For each, tell me whether the technique scales DOWN to local 8B
models on 8 GB VRAM or whether it strictly needs frontier models.

---

### TRACK 2 — Local Inference at 8 GB VRAM (RTX 4060) — frontier 2026

**Question:** What's the SOTA recipe for fast, high-quality local
code-generation inference on consumer hardware in 2026?

Specifically:
- **llama.cpp** state of the art:
  - Latest verified-working `--cache-reuse` configuration; any
    regressions through 2026?
  - **Speculative decoding** with a draft model (Qwen2.5-Coder-0.5B
    drafting Qwen2.5-Coder-7B-target on RTX 4060) — what's the
    measured net throughput gain in 2026? Are there sub-1B drafters
    optimized for code?
  - **Q4_K_M vs Q5_K_M vs IQ4_NL vs Q3_K_M** for code models on
    8 GB — perplexity vs throughput tradeoff with current numbers.
  - KV cache quantization (Q4_0 KV) — confirmed safe on FA fast path
    in current builds?
  - GPU graphs (`GGML_CUDA_GRAPHS=1`) — still a win in 2026?
- **llama-swap** evolution: any successor / better orchestrator for
  rotating models on a single GPU? `--continuous-batching` integration?
- **Model selection 2026** for local code:
  - Qwen3-Coder family (sizes, context, license, benchmarks)
  - DeepSeek-Coder-V3 / V4 if released
  - StarCoder3 / DeepSeek-R1-distill-coder variants
  - Mercury Coder (Inception Labs) — diffusion-based code gen,
    practical on consumer GPUs?
  - Llama 4 Code if it exists by 2026
  Give me the concrete pick + size + quant for an RTX 4060.
- **CPU fallback / hybrid**: Are there 2026 patterns for offloading
  attention to CPU while keeping FFN on GPU that materially help
  8 GB cards?
- **Inference-time reasoning** (o1-style) on local models — viable
  for 8B-class? Or strictly large-model-only?

---

### TRACK 3 — Real Tool Use (sandboxes, MCP, agentic actions)

**Question:** How do frontier 2026 agentic systems actually USE tools
(not just emit tool-call JSON), and what's the secure orchestration
layer for a single-host AMOR-class deployment?

Specifically:
- **MCP (Model Context Protocol) ecosystem 2026**:
  - Which MCP servers are production-grade and worth shipping with
    AMOR? (filesystem, git, github, postgres, slack, browser,
    docker, kubernetes, etc.)
  - Anthropic's reference implementations vs. community ones — quality
    audit by 2026.
  - **MCP-server-everything**, **mcp-host** orchestration patterns.
- **Sandbox isolation 2026**:
  - Docker + `--cap-drop=ALL` + seccomp + tecnativa/docker-socket-proxy
    — still the default, or has gVisor/Kata become consumer-viable?
  - **Firecracker microVMs** for code execution (production setups
    using them in 2026?).
  - **WebAssembly** (Wasmtime, WasmEdge) for sub-second startup —
    practical for code-gen sandboxes by 2026?
  - **Snapshot-based startup** (CRIU, restorables) for sub-100ms
    sandbox cold start.
- **Browser automation as a tool**:
  - Playwright vs Browser Use vs WebVoyager 2026 — which one is the
    workhorse for "fetch this docs page" / "test this UI" tool calls?
  - Anti-detection / CAPTCHA handling status 2026.
- **Filesystem + git as first-class tools**:
  - How does Aider / Cline / Cursor 2026 handle multi-file edits +
    rollback? Diff-apply patterns; SEARCH/REPLACE vs unified-diff
    vs AST-based.
- **Permission / safety layer**:
  - Approval flows for destructive ops (rm, git push, kubectl apply)
    — 2026 best practice?
  - Capability tokens / runtime ACL for tool calls.
- **Code review tools as agents**:
  - Semgrep, CodeQL, Ruff/Bandit/mypy integration patterns —
    feed-as-context vs first-class tools.

---

### TRACK 4 — Domain Awareness + Requirement Elaboration

**Question:** Frontier systems in 2026 turn vague prompts ("snake game")
into rich, complete production deliverables (canvas + animations +
mobile + score + restart + dark theme). What's the architecture?

Specifically:
- **"Specification-first" code-gen** (CodeIt, Codeium, Cursor):
  - Do they elaborate user intent into a structured spec before
    coding? What format (JSON schema, Lean-style spec, BDD scenarios)?
  - Has LLM-driven requirement elicitation matured into a deployable
    pattern by 2026?
- **Skill libraries / template injection** (Anthropic Agent Skills,
  Cursor Rules, Claude Projects):
  - How do they encode domain knowledge ("a snake game NEEDS canvas
    + score + restart") without hard-coding rules?
- **Retrieval-augmented domain context**:
  - RAG over best-practice patterns / canonical implementations —
    embedding model + retrieval pattern that works with 8 GB VRAM.
- **Self-reflection on completeness**:
  - Verifier-based checks ("does this snake game include game over?")
    — LLM-as-judge prompts that reliably detect missing features.
- **Empirical benchmarks for "production quality"**:
  - Is there a 2026 benchmark beyond pass@k that measures
    completeness, accessibility, mobile-readiness, etc.?
- **Hard-coded best-practice templates vs LLM elicitation**:
  - Cost-benefit: when does it make sense to ship a static
    `snake_game.template.md` vs trust the LLM to elaborate?

---

### TRACK 5 — Code Quality, Tests-as-Truth, and Production Hardening

**Question:** What's the 2026 SOTA for **automatic** quality assurance
of LLM-generated code — beyond pass@1, beyond critic LLMs?

Specifically:
- **Test-driven generation** (write tests first, then code) — does
  it actually improve final pass rate for 8B-class models? Numbers.
- **Property-based testing** (Hypothesis, Hedgehog) integration with
  LLM gen — has anyone made this push-button by 2026?
- **Mutation testing on AI-generated code** — viable feedback signal
  for the agent loop, or too slow to be practical?
- **Static analysis as agent context** (Semgrep, Ruff, Bandit, mypy
  in strict mode) — 2026 patterns for feeding linter errors back
  into the coder agent.
- **Symbolic / formal verification lite**:
  - Has Crosshair / Daikon-style invariant inference reached
    practicality for AI-generated Python by 2026?
  - Liquid Haskell / Z3 integration for AI gen — anyone shipping?
- **Test execution sandboxing**:
  - Coverage-guided test generation (similar to fuzzing) for AI code
    — anything reaching production in 2026?
- **SWE-bench Verified 2026 results**:
  - Top open-source / open-weight system on SWE-bench Verified at
    end of 2026. Compare to closed-source frontier (Claude 4.7,
    Gemini 2.5, GPT-5). Cost vs quality.

---

### TRACK 6 — Hardware-Aware Multi-Agent Orchestration

**Question:** How do you run a 5-agent (planner / coder / tester /
debugger / critic) pipeline on a single 8 GB GPU without thrashing,
without sequential bottlenecks, and without sacrificing the
specialization advantage?

Specifically:
- **Model rotation strategies 2026**:
  - llama-swap successor patterns, eviction policies, warm pools.
  - **LoRA hot-swap** as the alternative to full-model swap — is
    `--lora-hot-swap` mature in llama.cpp by 2026? Per-role LoRA on
    a single base model — measured quality vs single-shared-model.
- **Async pipeline parallelism**:
  - Can the static-analysis phase run concurrently with the test-gen
    phase on the same GPU? Latency-budget patterns.
- **CPU/GPU task partitioning**:
  - 2026 best practice for putting embedding + reranking + simple
    classification on CPU while keeping the 7-8B model on GPU.
- **Speculative decoding with mixed-role drafters**:
  - Can a 0.5B "fast critic" draft tokens that the main coder model
    accepts/rejects? Has anyone shipped this pattern?
- **Cache management across agent calls**:
  - Prefix-cache hit rate for "system prompt + plan + spec" prefix
    that ALL downstream agents share — measured savings on RTX 4060.
- **Batching strategies**:
  - `--continuous-batching` + `--parallel 2` for two concurrent users
    on a single 8 GB card — practical, or too memory-hungry?

---

## 📦 Beklenen output formatı

For each track, give me:

1. **Executive summary** (3-5 sentences)
2. **Top sources table** (paper / project / blog with link, year, key
   contribution, RTX 4060 viability flag)
3. **Recommendations for AMOR** ranked by ROI:
   - QUICK WIN (≤1 week, 8 GB-friendly, low risk)
   - MEDIUM (2-4 weeks, requires careful integration)
   - LONG GAME (research-grade, defer)
4. **Anti-patterns to avoid** with one-line rationale per item
5. **Open questions** the research didn't fully resolve

At the end of all six tracks, produce a **final synthesis**:
- The 5 highest-impact upgrades AMOR should ship first
- The 3 things AMOR is currently doing that are WRONG / outdated
- A concrete 90-day roadmap mapping research findings to
  implementation cycles

Cite every numerical claim. Prefer 2026 sources; 2025 OK; 2024 only
if still SOTA. Reject pre-2024 sources unless they're foundational.

Treat this as if you were briefing an engineering lead about to spend
3 months building. Be specific. Be opinionated. No hedging.
