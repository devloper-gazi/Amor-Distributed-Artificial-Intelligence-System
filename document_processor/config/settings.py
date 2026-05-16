"""
Configuration management using Pydantic for type-safe settings.
All settings can be overridden via environment variables.
"""

from pydantic_settings import BaseSettings
from typing import Optional
from pathlib import Path


class Settings(BaseSettings):
    """Application settings with environment variable support."""

    # Service Configuration
    service_name: str = "document-processor"
    environment: str = "production"
    log_level: str = "INFO"
    debug: bool = False

    # Processing Configuration
    max_concurrent_sources: int = 1000
    chunk_size_bytes: int = 1024 * 1024  # 1MB
    batch_size: int = 1000
    worker_count: int = 4
    max_retries: int = 3

    # Kafka Configuration
    kafka_bootstrap_servers: str = "localhost:9092"
    kafka_topic: str = "documents"
    kafka_group_id: str = "processors"
    kafka_partitions: int = 50
    kafka_replication_factor: int = 3
    kafka_max_poll_records: int = 1000
    kafka_session_timeout_ms: int = 30000

    # Redis Configuration
    redis_host: str = "localhost"
    redis_port: int = 6379
    redis_db: int = 0
    redis_ttl: int = 300  # 5 minutes
    redis_max_connections: int = 50
    redis_password: Optional[str] = None

    # PostgreSQL Configuration
    postgres_host: str = "localhost"
    postgres_port: int = 5432
    postgres_database: str = "docdb"
    postgres_user: str = "user"
    postgres_password: str = "pass"
    postgres_pool_size: int = 20
    postgres_max_overflow: int = 10

    @property
    def postgres_url(self) -> str:
        """Build PostgreSQL connection URL."""
        return (
            f"postgresql+asyncpg://{self.postgres_user}:{self.postgres_password}"
            f"@{self.postgres_host}:{self.postgres_port}/{self.postgres_database}"
        )

    # MongoDB Configuration
    mongo_host: str = "localhost"
    mongo_port: int = 27017
    mongo_database: str = "documents"
    mongo_user: Optional[str] = None
    mongo_password: Optional[str] = None
    mongo_max_pool_size: int = 100

    @property
    def mongo_url(self) -> str:
        """Build MongoDB connection URL."""
        if self.mongo_user and self.mongo_password:
            return (
                f"mongodb://{self.mongo_user}:{self.mongo_password}"
                f"@{self.mongo_host}:{self.mongo_port}"
            )
        return f"mongodb://{self.mongo_host}:{self.mongo_port}"

    # Translation API Keys
    google_translate_api_key: Optional[str] = None
    azure_translator_key: Optional[str] = None
    azure_translator_region: str = "eastus"
    anthropic_api_key: Optional[str] = None

    # Rate Limits (requests per minute)
    google_translate_rpm: int = 1000
    azure_translate_rpm: int = 2000
    anthropic_rpm: int = 50
    web_scraping_rpm: int = 100

    # Translation Configuration
    translation_quality_threshold: float = 0.85
    translation_cache_enabled: bool = True
    translation_batch_size: int = 10

    # Language Detection
    fasttext_model_path: str = "lid.176.bin"
    language_detection_confidence_threshold: float = 0.5

    # Monitoring Configuration
    prometheus_port: int = 9090
    enable_tracing: bool = True
    enable_metrics: bool = True
    metrics_push_interval: int = 10  # seconds

    # Jaeger Tracing
    jaeger_agent_host: str = "localhost"
    jaeger_agent_port: int = 6831

    # Bloom Filter Configuration
    bloom_filter_capacity: int = 1_000_000
    bloom_filter_error_rate: float = 0.01

    # Circuit Breaker Configuration
    circuit_breaker_failure_threshold: int = 5
    circuit_breaker_recovery_timeout: int = 60  # seconds

    # Retry Configuration
    retry_max_attempts: int = 5
    retry_min_wait: int = 2  # seconds
    retry_max_wait: int = 60  # seconds

    # Storage Configuration
    storage_backend: str = "local"  # local, s3, minio
    storage_path: Path = Path("/data/documents")
    s3_bucket: Optional[str] = None
    s3_region: str = "us-east-1"
    s3_access_key: Optional[str] = None
    s3_secret_key: Optional[str] = None

    # Web Scraping Configuration
    web_timeout: int = 30  # seconds
    web_max_redirects: int = 5
    web_user_agent: str = "DocumentProcessor/1.0"
    playwright_headless: bool = True
    playwright_timeout: int = 30000  # milliseconds

    # PDF Processing Configuration
    pdf_ocr_enabled: bool = True
    pdf_ocr_languages: str = "eng+spa+fra+deu+ara+zho+hin+rus+jpn+kor"
    pdf_dpi: int = 300
    pdf_extract_images: bool = True
    pdf_extract_tables: bool = True

    # API Client Configuration
    api_timeout: int = 30
    api_max_retries: int = 3
    api_pool_connections: int = 10
    api_pool_maxsize: int = 20

    # File Processing Configuration
    file_chunk_size: int = 8192
    max_file_size_mb: int = 100
    supported_file_formats: list = [
        "csv", "json", "xml", "xlsx", "xls",
        "docx", "doc", "txt", "pdf"
    ]

    # Dead Letter Queue Configuration
    dlq_enabled: bool = True
    dlq_topic: str = "documents-dlq"
    dlq_max_retries: int = 3

    # Health Check Configuration
    health_check_interval: int = 30  # seconds

    # ---------------------------------------------------------------------
    # Authentication / Security
    # ---------------------------------------------------------------------
    auth_jwt_secret: str = "change-me-in-production-set-via-AUTH_JWT_SECRET"
    auth_jwt_algorithm: str = "HS256"
    auth_access_token_ttl_minutes: int = 15
    auth_refresh_token_ttl_days: int = 14
    auth_cookie_name: str = "amor_refresh"
    auth_cookie_secure: bool = False  # set True behind HTTPS
    auth_cookie_samesite: str = "lax"
    auth_max_failed_logins: int = 8
    auth_lockout_minutes: int = 15
    auth_ip_login_per_minute: int = 20
    auth_register_per_hour_per_ip: int = 10
    auth_password_min_length: int = 10

    # ─── Phase 1 optimization (fancy-swinging-karp) ─────────────
    # Pre-LLM relevance gate. Conservative defaults — the cap per tier
    # is intentionally HIGHER than today's effective LLM-survival count
    # so the filter never gets blamed for losing good content. The
    # existing post-LLM keep-filter (`relevance >= 0.22 and findings`)
    # in advanced_researcher.analyze() stays as a second-line gate.
    enable_relevance_prefilter: bool = True
    relevance_prefilter_fail_open: bool = True
    relevance_prefilter_debug: bool = False
    relevance_prefilter_min_score: float = 0.15

    # Per-tier hard caps. basic short-circuits to passthrough inside
    # the filter regardless of this number; medium uses cap == today's
    # max_total_sources (light effect); deep/expert/ultra prune.
    relevance_prefilter_max_sources_basic: int = 8
    relevance_prefilter_max_sources_medium: int = 25
    relevance_prefilter_max_sources_deep: int = 60
    relevance_prefilter_max_sources_expert: int = 100
    relevance_prefilter_max_sources_ultra: int = 120

    # Per-tier analyze() concurrency. Local Ollama tolerates ~2–3
    # concurrent generates before throughput collapses (KV-cache
    # contention on a single qwen2.5:7b instance). Numbers here are
    # safe defaults; bump via env if Ollama runs on faster hardware.
    analyze_concurrency_basic: int = 1
    analyze_concurrency_medium: int = 2
    analyze_concurrency_deep: int = 2
    analyze_concurrency_expert: int = 3
    analyze_concurrency_ultra: int = 3

    # Optional LLM response cache (opt-in). When enabled, identical
    # (model, system, prompt, max_tokens, temp) tuples skip Ollama and
    # serve from Redis. Default OFF to keep behaviour identical to
    # pre-Phase-1 until a user explicitly opts in.
    llm_response_cache_enabled: bool = False
    llm_response_cache_ttl_seconds: int = 7 * 24 * 3600

    # ── Code Intelligence Mode ──────────────────────────────────────────
    # Multi-agent local-only code engine — see document_processor/
    # code_intelligence/. Zero external API calls; all inference runs
    # against the local Ollama deployment.
    code_ollama_base_url: str = "http://ollama:11434"
    # Force a specific Ollama tag for ALL code agents. Empty = let the
    # CodeModelRegistry auto-select per role + effort.
    code_default_model: str = ""
    # Docker-based execution sandbox.
    code_sandbox_enabled: bool = True
    code_sandbox_timeout: int = 30
    code_sandbox_memory: str = "256m"
    # Phase 17 Commit M — engine forwards `dependencies` from the
    # plan spec block + coder metadata into the sandbox's
    # ``install_packages`` so e.g. snake-game-website code that
    # imports flask / pygame / requests actually pip-installs them
    # before running.  Allow-list-only sanitiser; cap per session.
    code_sandbox_pip_install_enabled: bool = True
    code_sandbox_max_pip_packages: int = 12
    # Phase 17 Commit T — DebuggerAgent emits SEARCH/REPLACE diffs
    # instead of rewriting the whole file (3-5x token savings on
    # 500-LOC outputs + fewer regressions in untouched lines).
    # Falls back to whole-file rewrite when the patch doesn't
    # apply cleanly (drift / ambiguous match / malformed fence).
    code_debug_diff_mode_enabled: bool = True
    # Maximum debug→fix→reexecute loops per session.
    code_max_debug_iterations: int = 3
    # Cycle D — quality-improvement Reflexion loop.  Distinct from
    # ``code_max_debug_iterations`` (which only fires on FAILED
    # execution).  After the pipeline reaches "review" the engine
    # scores the deliverable; if the score is below
    # ``code_reflexion_quality_threshold`` AND we still have
    # iterations available, the coder is re-invoked with a
    # feedback-rich prompt (sandbox stdout/stderr + critic issues +
    # test execution failures) to produce an improved version.  The
    # better-scored version wins.  Default 1 iteration is a balance
    # between cost (extra LLM call + extra sandbox run = ~30-60s) and
    # quality lift; production deployments can set 0 to disable.
    code_max_reflexion_iterations: int = 1
    code_reflexion_quality_threshold: int = 80
    # Cycle F Sprint 2 — Hypothesis property-based testing + branch
    # coverage Reflexion signal.  Both are FEEDBACK SIGNALS that
    # surface in the reflexion-retry prompt; they do NOT change the
    # quality-score weighting (35+25+15+25=100 from Cycle D is kept).
    #
    #   code_property_tests_enabled: when True, the Tester agent's
    #     prompt is augmented (Python only) to require @given
    #     invariants in addition to example-based tests.  Set False
    #     for a flag-flip rollback to Cycle-D-style tester output.
    #
    #   code_branch_coverage_threshold: 0.0-1.0 fraction; when
    #     pytest-cov reports below this, the Reflexion loop bundles
    #     a MISSED_BRANCHES block into the coder-retry prompt.
    #     Threshold is "feedback hint" not "hard gate".
    code_property_tests_enabled: bool = True
    code_branch_coverage_threshold: float = 0.80
    # Cycle F Sprint 3 — LoRA hot-swap via llama.cpp PR #10994
    # `"lora": [{"id": <int>, "scale": <float>}, ...]` per-request
    # body field.  ``code_lora_enabled`` is the master gate; when
    # False, the runtime never attaches a `lora` field to the
    # OpenAI-compat body (zero behaviour change from Sprint 2).
    #
    # ``code_lora_role_adapters`` maps role -> adapter ID as a JSON
    # string (parsed lazily so a bad value doesn't crash startup).
    # Examples:
    #   '{"coder":0,"tester":1,"debugger":2}'
    #   '{"coder":0}'              # only coder gets a LoRA
    #   '{}'                       # explicit empty (= disabled per-role)
    # The adapter IDs MUST match the order llama-swap mounts them
    # via the --lora flags in compose/llama-swap/config.yaml.
    code_lora_enabled: bool = False
    code_lora_role_adapters: str = "{}"
    code_lora_default_scale: float = 1.0
    # Cycle F Sprint 4 — Anthropic Agent Skills loader.
    #   code_skills_enabled: master gate.  When False, the planner
    #     system prompt is unchanged from Sprint 3.
    #   code_skills_root: filesystem directory containing
    #     {skill_name}/SKILL.md.  Repo-relative by default.
    #   code_skills_token_budget: max token cost (estimated at
    #     ~4 chars/token) for the rendered SKILLS AVAILABLE: block.
    #     When the library outgrows the budget, render_skill_index
    #     drops the most-expensive skills first.
    code_skills_enabled: bool = False
    code_skills_root: str = "skills"
    code_skills_token_budget: int = 2000
    # Cycle F Sprint 5 — ApprovalPolicy gate for MCP tool dispatch.
    # When `code_approval_enabled=False` (default), every tool call
    # runs as in Sprints 1-4.  When True, dispatch passes through
    # `ApprovalPolicy.decide()` first; tools in `code_approval_deny`
    # are blocked outright, `code_approval_allow_silent` runs without
    # prompt, and everything else routes through the SSE bridge for
    # human approval.
    #
    # CSV-string for lists: "name1,name2,name3" (whitespace tolerated).
    # JSON-string for category_actions: {"delete": "deny", "exec": "prompt"}
    # Cost circuit-breaker: per-session token budget; tripped session
    # blocks all subsequent tool calls until reset.
    code_approval_enabled: bool = False
    code_approval_allow_silent: str = ""   # CSV of tool names
    code_approval_deny: str = ""           # CSV of tool names
    code_approval_prompt: str = ""         # CSV of tool names
    code_approval_default_action: str = "prompt"  # allow | prompt | deny
    code_approval_category_actions: str = ""  # JSON dict
    code_approval_cost_budget_tokens: int = 50_000
    # Cycle F Sprint 6 — async pipeline parallelism.
    # When True, _phase_test runs concurrently with _phase_execute +
    # _phase_analyze after _phase_implement completes (currently
    # execute + analyze are already concurrent via asyncio.gather;
    # this adds test to the same group).  Cuts pipeline wall by
    # ~25-30s on the Sprint-0 corpus median case.  Rollback: flip
    # to False — pipeline reverts to Cycle D's sequential test
    # phase.
    #
    # `code_critic_prefix_warmup` fires a non-blocking critic
    # prefix-cache warmup as soon as `self.code` is available so
    # the review phase's first LLM call lands on a hot KV cache.
    # Best-effort: failures silently degrade to the cold-prefix
    # case.  Forward-compat for Sprint 6 piece 2b (critic async).
    code_pipeline_parallel: bool = True
    code_critic_prefix_warmup: bool = True
    # v18.1 Step 4 (Cycle G) — fully-async critic.  When True
    # (default), the critic LLM call is kicked off as a background
    # task right after the parallel block (execute/analyze/test)
    # completes, in parallel with the debug retry loop.  The review
    # phase then awaits the task with a freshness timeout
    # (`code_critic_async_timeout_s`), falling back to
    # `approved_with_minor` score=70 when the call stalls.  Saves
    # ~30-60s on the Sprint-0 corpus median (Phi-4 Q4_K_M critic
    # latency moves entirely off the critical path on Build prompts
    # where debug retries inflate wall-clock by 100-300s).
    # Flag-flip rollback to v18 inline-blocking critic behaviour.
    code_critic_async: bool = True
    code_critic_async_timeout_s: float = 8.0
    # v18.1.2 (Cycle G) — sandbox tmpfs ceiling.  HumanEval+ 50 runs
    # circa 2026-05-05 hit ENOSPC during `pip install --target=
    # /tmp/pip-prefix numpy` because the previous 384m default was
    # just barely large enough for numpy alone (~75 MB installed +
    # transient wheel staging).  Doubling to 768m absorbs scipy /
    # sympy combos and pip's tempdir overhead on the same /tmp
    # without going to gigabyte territory.  Operators on tight RAM
    # budgets can override via env `AMOR_CODE_SANDBOX_TMPFS_SIZE_MB`.
    code_sandbox_tmpfs_size_mb: int = 768
    # Cycle G G3 — CodeQL hot-path integration.  Default OFF because
    # the CodeQL CLI (~600 MB bundle) is NOT shipped with
    # python:3.11-slim; operator installs it host-side and flips
    # this flag on.  When enabled, the StaticAnalysisHarness'
    # gather block calls `_run_codeql()` against every Python
    # snippet >200 LOC, mapping SARIF findings to AnalysisIssue
    # with severity="security" so they feed `_score_candidate`'s
    # 15-pt static-analysis slot.
    code_codeql_enabled: bool = False
    # Auto-pull the best code model if not installed.
    code_auto_pull_models: bool = True
    # Redis TTL for in-flight code intelligence sessions.
    code_session_ttl_seconds: int = 7200
    # Comma-separated language images to pre-pull at startup so the
    # first execution isn't slowed by a 100 MB image fetch.
    code_sandbox_prewarm_images: str = "python:3.11-slim,node:20-slim"
    # v17 PR #5 — sandbox warm-pool scaffolding.  ``size=0`` disables
    # the pool entirely (current behaviour: a fresh container per
    # ``execute()``).  Set ``size>=1`` to opt in to a future
    # ``SandboxPool`` that pre-creates ``size`` ``docker create`` 'd
    # containers per language and reuses them via ``docker exec``.
    # Pool implementation is staged for the follow-up commit; the
    # settings + telemetry foundations land first so operators can
    # measure cold-start before deciding whether to flip it on.
    code_sandbox_pool_size: int = 0
    # Comma-separated list of languages to pre-create pool slots for.
    code_sandbox_pool_languages: str = "python,javascript"
    # Seconds to wait for a free pool slot before falling back to the
    # ephemeral ``docker run`` path.  Prevents pool exhaustion from
    # blocking the engine indefinitely.
    code_sandbox_lease_timeout_s: float = 5.0
    # Recycle a pooled container after this many leases — bounds the
    # accumulation of /tmp residue + pip caches across runs.
    code_sandbox_max_lease_count: int = 50

    # ── v2: Capability Discovery (autonomous self-extension) ──────────
    code_capability_discovery_enabled: bool = True
    code_capability_discovery_interval_seconds: int = 3600
    code_capability_discovery_max_per_cycle: int = 3

    # ── v2: Observability (Langfuse / OpenLLMetry) ─────────────────────
    code_langfuse_url: str = ""
    code_langfuse_public_key: str = ""
    code_langfuse_secret_key: str = ""

    # ── v10: Code Synthesis Reactor ────────────────────────────────────
    # Master toggle for the empirical-perf + RAG + tournament + bandit
    # layer that sits on top of the Multi-ML Mesh. Each individual
    # feature can also be opted out via `code_reactor_features`.
    code_reactor_enabled: bool = True
    # Comma-separated set of enabled reactor features. Subset of:
    #   benchmarker, symbolic_complexity, tournament, property_tests,
    #   rag, llm_cache, bandit
    code_reactor_features: str = (
        "benchmarker,symbolic_complexity,tournament,property_tests,"
        "rag,llm_cache,bandit"
    )
    # TournamentRunner — number of parallel candidate impls per session.
    code_tournament_n: int = 3
    code_tournament_max: int = 5
    # PropertyTestGenerator — also ask the LLM to suggest invariants.
    code_property_tests_llm_suggest: bool = True
    # PerformanceBenchmarker — input scales for the progressive bench.
    code_bench_scales: str = "10,100,1000,10000"
    code_bench_timeout_per_scale_s: int = 8
    # CodeCorpusRAG — top-K retrieval + similarity floor.
    code_rag_top_k: int = 3
    code_rag_similarity_floor: float = 0.55
    # SemanticLLMCache — TTL + cosine threshold + invalidation salt.
    code_llm_cache_ttl_s: int = 86_400
    code_llm_cache_cosine_threshold: float = 0.92
    # Bumping this value invalidates every cached LLM call. Bump
    # whenever a system prompt changes meaningfully.
    code_reactor_cache_salt: int = 1
    # SpecialistBandit — exploration knob + cold-start floor.
    code_bandit_temperature: float = 1.0
    code_bandit_cold_start_threshold: int = 5

    # ── Cognitive upgrade — Phase 1A modules ─────────────────────────────
    # The .env.example carries human-readable docs; this block mirrors
    # the names so pydantic-settings can populate them. Each is fail-soft
    # — disabling a flag falls back to the previous behaviour.
    logic_engine_strategy: str = "rule_based"
    z3_verification_enabled: bool = True
    z3_timeout_seconds: int = 30
    z3_max_retries: int = 3
    episodic_memory_enabled: bool = True
    episodic_reuse_threshold: float = 0.85
    episodic_seed_threshold: float = 0.60
    episodic_top_k: int = 3
    rlef_enabled: bool = True
    rlef_min_pass_rate: float = 0.8
    rlef_fast_threshold_ms: float = 5000.0
    rlef_kafka_topic: str = "task.rlef_reward"
    rlef_mongo_collection: str = "rlef_rewards"
    # Master gate for Phase 1B (engine integration of the four Phase 1A
    # modules into QuickCodeEngine). Setting this to False keeps the
    # modules importable + tested but the engine never calls into them
    # — useful for isolating regressions during a tricky upgrade.
    cognitive_phase_1b_enabled: bool = True

    # ── Quick Code V2 (Adapter pattern over the existing engine) ─────────
    # Master gate. When False the engine bypasses every V2 phase and the
    # behaviour is byte-identical to pre-V2 — the safety net for any
    # regression bisect. Per-feature flags below toggle individual modules
    # so a single misbehaving component can be isolated without disabling
    # the whole layer.
    quick_v2_enabled: bool = True
    # TaskClassifier — 1.5B router that classifies trivial / simple /
    # complex / math and (when set) auto-redirects "complex" tasks back
    # to the Pro Code Intelligence engine.
    quick_v2_router_enabled: bool = True
    quick_v2_router_model: str = "qwen2.5:1.5b"
    quick_v2_router_redirect_to_pro: bool = True
    # Striatum — Redis cosine ≥ 0.95 fast-path procedural cache.
    quick_v2_striatum_enabled: bool = True
    quick_v2_striatum_threshold: float = 0.95
    quick_v2_striatum_ttl_s: int = 86_400
    # Bump to invalidate every Striatum cache entry (e.g. after a prompt
    # template change).
    quick_v2_striatum_salt: int = 1
    # SkCoder — BM25 + cosine hybrid retrieval. α below the floor falls
    # back to a "<FILL_HERE:hint>" placeholder so the coder template can
    # ask the LLM to fill the gap.
    quick_v2_sk_enabled: bool = True
    quick_v2_sk_alpha_floor: float = 0.35
    quick_v2_sk_top_k: int = 5
    # SymCode — SymPy subprocess validator (math tasks only).
    quick_v2_symcode_enabled: bool = True
    quick_v2_symcode_timeout_s: int = 10
    # ParselDecomposer — divide-and-conquer + Design-by-Contract.
    quick_v2_parsel_enabled: bool = True
    quick_v2_parsel_max_depth: int = 2
    # MCTS UCT (C=1.41) — gated to mode='pro'; quick mode default-off.
    quick_v2_use_mcts: bool = False
    quick_v2_mcts_max_iters: int = 16
    quick_v2_mcts_c: float = 1.41
    # SeekerDebugger — 5-agent Scanner→Detector→Predator→Ranker→Handler
    # pipeline. Replaces the monolithic DebuggerAgent when enabled.
    quick_v2_use_seeker: bool = True
    # Anton-Brain — pure prompt shaping (no LLM calls). Budgets ~3200
    # tokens across IDENTITY / GLOBAL_RULES / TASK_CONTEXT / ERROR_MEMORY.
    quick_v2_anton_brain_enabled: bool = True
    quick_v2_anton_brain_budget: int = 3200
    # ORPO preference-pair export. Ships off by default — flip on when
    # the offline trainer is ready to consume the collection.
    quick_v2_orpo_enabled: bool = False
    quick_v2_orpo_collection: str = "orpo_pairs"
    # Sandbox tier limits. Quick is the default; Pro doubles memory and
    # tripples the timeout for harder workloads.
    quick_v2_sandbox_quick_mem_mb: int = 256
    quick_v2_sandbox_quick_timeout_s: int = 15
    quick_v2_sandbox_pro_mem_mb: int = 512
    quick_v2_sandbox_pro_timeout_s: int = 45
    # ── Hardware-incompatible flags. Default OFF on this 8 GB GPU host.
    # Flip them on hosts with ≥ 24 GB GPU memory; the route layer will
    # still raise 503 if the model is not pulled.
    quick_v2_specialist_32b_enabled: bool = False  # requires ≥24GB GPU
    quick_v2_specialist_32b_model: str = ""
    quick_v2_speculative_decoding_enabled: bool = False  # requires the 32B specialist
    quick_v2_speculative_draft_model: str = "qwen2.5:1.5b"

    # ── Sentinel — multi-agent local security intelligence (V1) ─────────
    # Master gate.  Per-feature flags toggle individual stages so a
    # misbehaving piece can be disabled without taking down the whole
    # module.
    sentinel_enabled: bool = True
    sentinel_router_redirect_complex: bool = True
    sentinel_static_swarm_enabled: bool = True
    sentinel_ml_pipeline_enabled: bool = True
    sentinel_rag_enabled: bool = True
    sentinel_rag_table_cwe: str = "sentinel_cwe"
    sentinel_rag_table_owasp: str = "sentinel_owasp"
    sentinel_rag_table_history: str = "sentinel_history"
    sentinel_rag_table_project: str = "sentinel_project"
    sentinel_critic_loop_enabled: bool = True
    sentinel_critic_loop_max_iters: int = 3
    sentinel_self_play_enabled: bool = False
    sentinel_auditor_voting_n: int = 3
    sentinel_auditor_temperature: float = 0.2
    sentinel_reasoner_temperature: float = 0.5
    sentinel_redteam_temperature: float = 0.7
    sentinel_patcher_temperature: float = 0.2
    sentinel_judge_temperature: float = 0.0
    sentinel_auditor_model: str = "qwen2.5-coder:7b"
    sentinel_reasoner_model: str = "qwen2.5:7b"
    sentinel_redteam_model: str = "qwen2.5-coder:7b"
    sentinel_patcher_model: str = "qwen2.5-coder:7b"
    sentinel_judge_model: str = "qwen2.5:7b"
    sentinel_embedding_model: str = "nomic-ai/nomic-embed-text-v1.5"
    sentinel_default_scan_profile: str = "standard"
    sentinel_quick_timeout_s: int = 60
    sentinel_standard_timeout_s: int = 240
    sentinel_deep_timeout_s: int = 900
    sentinel_paranoid_timeout_s: int = 1800
    sentinel_mongo_findings_collection: str = "sentinel_findings"
    sentinel_mongo_calibration_collection: str = "sentinel_models_calibration"
    sentinel_kafka_topic: str = "task.sentinel_finding"
    sentinel_max_repo_size_mb: int = 200
    sentinel_max_files_per_scan: int = 5_000
    # Hardware-flag opt-ins (off until upstream ML deps are pulled).
    sentinel_use_codebert_classifier: bool = False
    sentinel_use_xgboost_ranker: bool = False
    sentinel_use_isolation_forest: bool = False

    # ── Phase 15 — Evolution Engine (genome / ledger / proposals) ────
    sentinel_evolution_enabled: bool = True
    # Filesystem root for ledger.jsonl, prompts/, agents/, adapters/,
    # students/, rules/, architecture/, distillation/.  The path is
    # auto-created on first write.
    sentinel_evolution_root: str = "data/sentinel/evolution"
    sentinel_evolution_actor_default: str = "console"
    sentinel_evolution_max_ledger_page: int = 500
    sentinel_evolution_require_user_consent: bool = True
    # Per-subsystem manual-trigger gates.  Off by default — flipped
    # on by an operator from the Evolution Console.
    sentinel_evolution_allow_prompt_trigger: bool = True
    sentinel_evolution_allow_rule_trigger: bool = True
    sentinel_evolution_allow_spawn_trigger: bool = True
    sentinel_evolution_allow_dag_trigger: bool = True
    sentinel_evolution_allow_lora_trigger: bool = False  # opt-in
    sentinel_evolution_allow_distill_trigger: bool = False  # opt-in
    sentinel_evolution_allow_curriculum_trigger: bool = True

    # ── Phase 16 — Adapter Foundations (pluggable LLM backend) ───────
    # Selects which inference backend ``local_ai.llm_backend`` returns
    # from ``get_backend()``.  Allowed values:
    #   ""              — empty: fall through to AMOR_LLM_BACKEND env
    #                     (Cycle C Sprint 1 — env-driven rollback flag).
    #                     Resolves to "ollama" if env is also unset.
    #   "ollama"        — today's behaviour (forced)
    #   "llama-swap"    — llama-swap proxy (OpenAI /v1)
    #   "llama-cpp"     — direct llama-server (OpenAI /v1)
    #   "openai-compat" — generic /v1 (vLLM / ExLlamaV2 / LM Studio)
    #   "stub"          — deterministic test backend (used in CI)
    llm_backend: str = ""
    # Override URL for the active backend.  Empty string falls back
    # to ``OLLAMA_BASE_URL`` env var / per-backend default.
    llm_backend_url: str = ""

    # OpenAI-compatible /v1 facade master gate (Commit C).  Off here
    # makes external SDKs (Letta, OpenHands, Aider) unable to plug in
    # via OPENAI_BASE_URL=http://localhost:8000/v1, but does not break
    # any internal code path.
    openai_compat_enabled: bool = True

    # ── Phase 16 Commit D1 — RAG hybrid + reranker ──────────────────
    # Vector + BM25 retrieval with reciprocal-rank fusion.  Default
    # on — the heuristic keyword-overlap path that lived inside
    # ``LanceDBVectorStore.hybrid_search`` is replaced with the
    # production ``BM25`` from ``document_processor/rag/rag_engine.py``.
    rag_hybrid_search_enabled: bool = True
    # RRF constant ``k`` from the original RRF paper (Cormack et al.)
    # — 60 is the canonical default.
    rag_rrf_k: int = 60
    # BM25 hyperparameters; defaults match Okapi BM25 standard tunings.
    rag_bm25_k1: float = 1.5
    rag_bm25_b: float = 0.75
    # Cross-encoder reranker (sentence-transformers/ms-marco-MiniLM-
    # L-6-v2).  Off by default — flipping on adds ~150 ms per query
    # on CPU but visibly improves Recall@5 on long-tail questions.
    rag_reranker_enabled: bool = False
    # Maximum candidates fed to the reranker after the dense+BM25
    # fusion stage.  Larger means better quality and more latency.
    rag_reranker_top_k: int = 20

    # ── Phase 16 Commit D2 — embedder + late-chunking ───────────────
    # Default embedder.  The historical nomic-embed-text-v1.5 (768-dim)
    # remains the on-disk default so existing ``documents`` corpora
    # are unmoved.  Set to ``"BAAI/bge-m3"`` for the multilingual,
    # 1024-dim BGE-M3 embedder; the vector store auto-creates a fresh
    # ``documents_bge_m3_1024`` table.
    rag_embedding_model: str = "nomic-ai/nomic-embed-text-v1.5"
    # When True, non-default embedders land in their own per-model
    # table so writes never collide with the historical schema.  Off
    # only for advanced users who wipe + rebuild the index manually.
    rag_per_model_table: bool = True
    # Chunking strategy.  ``naive`` keeps the existing char-window
    # behaviour; ``late`` uses ``LateChunker`` to attach a leading-
    # window document context to each chunk's embed payload.
    rag_chunking_strategy: str = "naive"
    # Maximum length of the leading-window context attached by
    # late-chunking.  Aligns with BGE-M3's 8192-token sequence cap.
    rag_late_chunking_window: int = 8192

    # ── Phase 16 Commit E — MCP-style typed tool registry ──────────
    # Master gate for the ``/mcp/v1/*`` server.  Off by default —
    # operators flip on per-host once they've vetted what the
    # registry exposes (read_file / search_codebase / compile_check
    # / taint_trace / cve_lookup / exploit_sandbox).  ``/v1/*`` and
    # the in-process tool registry are always available; only the
    # external MCP client surface is gated.
    enable_mcp_server: bool = False

    # ── Phase 16 Commit F — Letta-style 3-tier memory ──────────────
    # Filesystem root for ``MemoryStore`` SQLite databases.  Each
    # session / scope gets its own subtree.
    memory_root: str = "data/amor_memory"
    # Recall (ring buffer) — last N turns kept in fast scratchpad.
    memory_recall_window: int = 50
    # Core (single-row blob) — always-in-context byte cap.
    memory_core_max_bytes: int = 2048
    # Reserved name for the archival LanceDB table once the
    # SQLite-backed archival is upgraded to vector-indexed storage.
    memory_archival_table: str = "amor_archival"
    # Append a ledger entry for every memory write so the immutable
    # trail covers conversation history too.  Off makes memory
    # writes silent (no Phase 15 ledger noise).
    memory_ledger_audit_enabled: bool = True

    class Config:
        """Pydantic configuration."""
        env_file = ".env"
        env_file_encoding = "utf-8"
        case_sensitive = False
        extra = "ignore"


# Global settings instance
settings = Settings()
