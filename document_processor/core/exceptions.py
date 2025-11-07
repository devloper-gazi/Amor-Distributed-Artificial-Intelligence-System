"""
Custom exceptions for the document processing system.
Provides specific error types for better error handling and logging.
"""


class DocumentProcessorException(Exception):
    """Base exception for all document processor errors."""
    def __init__(self, message: str, details: dict = None):
        super().__init__(message)
        self.message = message
        self.details = details or {}


class SourceProcessingError(DocumentProcessorException):
    """Error during source document processing."""
    pass


class WebScrapingError(SourceProcessingError):
    """Error during web scraping."""
    pass


class PDFProcessingError(SourceProcessingError):
    """Error during PDF processing."""
    pass


class DatabaseConnectionError(SourceProcessingError):
    """Error connecting to database."""
    pass


class APIClientError(SourceProcessingError):
    """Error in API client."""
    pass


class FileReadError(SourceProcessingError):
    """Error reading file."""
    pass


class LanguageDetectionError(DocumentProcessorException):
    """Error during language detection."""
    pass


class TranslationError(DocumentProcessorException):
    """Base exception for translation errors."""
    pass


class TranslationAPIError(TranslationError):
    """Error calling translation API."""
    def __init__(self, message: str, provider: str, status_code: int = None, details: dict = None):
        super().__init__(message, details)
        self.provider = provider
        self.status_code = status_code


class TranslationRateLimitError(TranslationError):
    """Translation API rate limit exceeded."""
    def __init__(self, message: str, provider: str, retry_after: int = None):
        super().__init__(message)
        self.provider = provider
        self.retry_after = retry_after


class TranslationQuotaExceededError(TranslationError):
    """Translation API quota exceeded."""
    def __init__(self, message: str, provider: str):
        super().__init__(message)
        self.provider = provider


class CacheError(DocumentProcessorException):
    """Error in cache operations."""
    pass


class QueueError(DocumentProcessorException):
    """Error in message queue operations."""
    pass


class StorageError(DocumentProcessorException):
    """Error in storage operations."""
    pass


class ConfigurationError(DocumentProcessorException):
    """Invalid configuration."""
    pass


class ValidationError(DocumentProcessorException):
    """Data validation error."""
    pass


class CircuitBreakerOpenError(DocumentProcessorException):
    """Circuit breaker is open, rejecting requests."""
    def __init__(self, service: str, message: str = None):
        msg = message or f"Circuit breaker open for {service}"
        super().__init__(msg)
        self.service = service


class RetryExhaustedError(DocumentProcessorException):
    """All retry attempts exhausted."""
    def __init__(self, message: str, attempts: int):
        super().__init__(message)
        self.attempts = attempts


class DeduplicationError(DocumentProcessorException):
    """Error in deduplication."""
    pass


class MonitoringError(DocumentProcessorException):
    """Error in monitoring/metrics."""
    pass
