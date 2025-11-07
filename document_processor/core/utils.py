"""
Utility functions for the document processing system.
"""

import hashlib
import re
from typing import Any, Optional, List
from datetime import datetime, timedelta
import asyncio
from functools import wraps
import time


def compute_hash(content: str, algorithm: str = "sha256") -> str:
    """
    Compute hash of content.

    Args:
        content: Content to hash
        algorithm: Hash algorithm (md5, sha1, sha256)

    Returns:
        Hex digest of hash
    """
    if algorithm == "md5":
        return hashlib.md5(content.encode("utf-8")).hexdigest()
    elif algorithm == "sha1":
        return hashlib.sha1(content.encode("utf-8")).hexdigest()
    else:
        return hashlib.sha256(content.encode("utf-8")).hexdigest()


def truncate_text(text: str, max_length: int = 1000, suffix: str = "...") -> str:
    """
    Truncate text to maximum length.

    Args:
        text: Text to truncate
        max_length: Maximum length
        suffix: Suffix to append if truncated

    Returns:
        Truncated text
    """
    if len(text) <= max_length:
        return text
    return text[: max_length - len(suffix)] + suffix


def clean_text(text: str) -> str:
    """
    Clean and normalize text.

    Args:
        text: Text to clean

    Returns:
        Cleaned text
    """
    # Remove excessive whitespace
    text = re.sub(r"\s+", " ", text)
    # Remove control characters
    text = re.sub(r"[\x00-\x1f\x7f-\x9f]", "", text)
    # Strip leading/trailing whitespace
    text = text.strip()
    return text


def chunk_text(text: str, chunk_size: int = 1000, overlap: int = 100) -> List[str]:
    """
    Split text into overlapping chunks.

    Args:
        text: Text to chunk
        chunk_size: Size of each chunk
        overlap: Overlap between chunks

    Returns:
        List of text chunks
    """
    chunks = []
    start = 0
    text_length = len(text)

    while start < text_length:
        end = start + chunk_size
        chunk = text[start:end]
        chunks.append(chunk)
        start = end - overlap

    return chunks


def estimate_tokens(text: str, chars_per_token: int = 4) -> int:
    """
    Estimate number of tokens in text.

    Args:
        text: Text to estimate
        chars_per_token: Average characters per token

    Returns:
        Estimated token count
    """
    return len(text) // chars_per_token


def format_bytes(bytes: int) -> str:
    """
    Format bytes as human-readable string.

    Args:
        bytes: Number of bytes

    Returns:
        Formatted string (e.g., "1.5 MB")
    """
    for unit in ["B", "KB", "MB", "GB", "TB"]:
        if bytes < 1024.0:
            return f"{bytes:.2f} {unit}"
        bytes /= 1024.0
    return f"{bytes:.2f} PB"


def format_duration(seconds: float) -> str:
    """
    Format duration as human-readable string.

    Args:
        seconds: Duration in seconds

    Returns:
        Formatted string (e.g., "1h 30m 45s")
    """
    if seconds < 60:
        return f"{seconds:.2f}s"

    minutes = seconds // 60
    seconds = seconds % 60

    if minutes < 60:
        return f"{int(minutes)}m {int(seconds)}s"

    hours = minutes // 60
    minutes = minutes % 60

    return f"{int(hours)}h {int(minutes)}m {int(seconds)}s"


def safe_divide(numerator: float, denominator: float, default: float = 0.0) -> float:
    """
    Safely divide two numbers, returning default if denominator is zero.

    Args:
        numerator: Numerator
        denominator: Denominator
        default: Default value if denominator is zero

    Returns:
        Division result or default
    """
    if denominator == 0:
        return default
    return numerator / denominator


async def async_retry(
    func,
    max_attempts: int = 3,
    delay: float = 1.0,
    backoff: float = 2.0,
    exceptions: tuple = (Exception,)
):
    """
    Retry async function with exponential backoff.

    Args:
        func: Async function to retry
        max_attempts: Maximum retry attempts
        delay: Initial delay in seconds
        backoff: Backoff multiplier
        exceptions: Exceptions to catch

    Returns:
        Function result

    Raises:
        Last exception if all retries fail
    """
    last_exception = None

    for attempt in range(max_attempts):
        try:
            return await func()
        except exceptions as e:
            last_exception = e
            if attempt < max_attempts - 1:
                sleep_time = delay * (backoff ** attempt)
                await asyncio.sleep(sleep_time)

    raise last_exception


def timing_decorator(func):
    """Decorator to measure async function execution time."""
    @wraps(func)
    async def wrapper(*args, **kwargs):
        start = time.time()
        try:
            result = await func(*args, **kwargs)
            return result
        finally:
            duration = (time.time() - start) * 1000
            print(f"{func.__name__} took {duration:.2f}ms")
    return wrapper


def extract_domain(url: str) -> Optional[str]:
    """
    Extract domain from URL.

    Args:
        url: URL to parse

    Returns:
        Domain name or None
    """
    pattern = r"(?:https?://)?(?:www\.)?([^/]+)"
    match = re.match(pattern, url)
    if match:
        return match.group(1)
    return None


def is_valid_url(url: str) -> bool:
    """
    Check if string is a valid URL.

    Args:
        url: URL to validate

    Returns:
        True if valid URL
    """
    url_pattern = re.compile(
        r"^https?://"  # http:// or https://
        r"(?:(?:[A-Z0-9](?:[A-Z0-9-]{0,61}[A-Z0-9])?\.)+[A-Z]{2,6}\.?|"  # domain
        r"localhost|"  # localhost
        r"\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3})"  # or IP
        r"(?::\d+)?"  # optional port
        r"(?:/?|[/?]\S+)$",
        re.IGNORECASE,
    )
    return url_pattern.match(url) is not None


def batch_items(items: List[Any], batch_size: int) -> List[List[Any]]:
    """
    Split items into batches.

    Args:
        items: Items to batch
        batch_size: Size of each batch

    Returns:
        List of batches
    """
    return [items[i : i + batch_size] for i in range(0, len(items), batch_size)]


def merge_dicts(*dicts: dict) -> dict:
    """
    Merge multiple dictionaries.

    Args:
        *dicts: Dictionaries to merge

    Returns:
        Merged dictionary
    """
    result = {}
    for d in dicts:
        result.update(d)
    return result


class RateLimitTracker:
    """Simple rate limit tracker."""

    def __init__(self, max_requests: int, time_window: int):
        """
        Initialize rate limit tracker.

        Args:
            max_requests: Maximum requests allowed
            time_window: Time window in seconds
        """
        self.max_requests = max_requests
        self.time_window = time_window
        self.requests = []

    def can_proceed(self) -> bool:
        """Check if request can proceed."""
        now = datetime.utcnow()
        cutoff = now - timedelta(seconds=self.time_window)

        # Remove old requests
        self.requests = [req for req in self.requests if req > cutoff]

        # Check if under limit
        if len(self.requests) < self.max_requests:
            self.requests.append(now)
            return True

        return False

    def time_until_available(self) -> float:
        """Get time until next request can proceed."""
        if not self.requests:
            return 0.0

        oldest = min(self.requests)
        available_at = oldest + timedelta(seconds=self.time_window)
        now = datetime.utcnow()

        if available_at <= now:
            return 0.0

        return (available_at - now).total_seconds()
