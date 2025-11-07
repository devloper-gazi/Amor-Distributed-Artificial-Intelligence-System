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

    class Config:
        """Pydantic configuration."""
        env_file = ".env"
        env_file_encoding = "utf-8"
        case_sensitive = False
        extra = "ignore"


# Global settings instance
settings = Settings()
