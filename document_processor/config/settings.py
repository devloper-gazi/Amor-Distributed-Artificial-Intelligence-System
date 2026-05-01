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
    # Maximum debug→fix→reexecute loops per session.
    code_max_debug_iterations: int = 3
    # Auto-pull the best code model if not installed.
    code_auto_pull_models: bool = True
    # Redis TTL for in-flight code intelligence sessions.
    code_session_ttl_seconds: int = 7200
    # Comma-separated language images to pre-pull at startup so the
    # first execution isn't slowed by a 100 MB image fetch.
    code_sandbox_prewarm_images: str = "python:3.11-slim,node:20-slim"

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

    class Config:
        """Pydantic configuration."""
        env_file = ".env"
        env_file_encoding = "utf-8"
        case_sensitive = False
        extra = "ignore"


# Global settings instance
settings = Settings()
