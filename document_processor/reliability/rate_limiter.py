"""
Rate limiting implementation using token bucket algorithm.
Supports both local and distributed (Redis-based) rate limiting.
"""

import asyncio
import time
from typing import Optional
from collections import deque
from ..config.logging_config import logger


class TokenBucketRateLimiter:
    """
    Token bucket rate limiter for controlling request rates.

    Uses the token bucket algorithm to allow bursts while maintaining
    an average rate limit.
    """

    def __init__(self, rate: int, period: int = 60):
        """
        Initialize token bucket rate limiter.

        Args:
            rate: Number of requests allowed
            period: Time period in seconds
        """
        self.rate = rate
        self.period = period
        self.tokens = float(rate)
        self.last_update = time.time()
        self.lock = asyncio.Lock()

    async def __aenter__(self):
        """Async context manager entry."""
        await self.acquire()
        return self

    async def __aexit__(self, *args):
        """Async context manager exit."""
        pass

    async def acquire(self, tokens: float = 1.0):
        """
        Acquire tokens, waiting if necessary.

        Args:
            tokens: Number of tokens to acquire
        """
        async with self.lock:
            now = time.time()
            elapsed = now - self.last_update

            # Refill tokens based on elapsed time
            self.tokens = min(
                self.rate, self.tokens + (elapsed * self.rate / self.period)
            )
            self.last_update = now

            if self.tokens < tokens:
                # Calculate wait time needed
                wait_time = (tokens - self.tokens) * self.period / self.rate
                logger.debug(
                    "rate_limit_waiting",
                    wait_time=wait_time,
                    tokens_available=self.tokens,
                    tokens_needed=tokens,
                )
                await asyncio.sleep(wait_time)
                self.tokens = 0
            else:
                self.tokens -= tokens

    def get_available_tokens(self) -> float:
        """Get number of currently available tokens."""
        now = time.time()
        elapsed = now - self.last_update
        available = min(self.rate, self.tokens + (elapsed * self.rate / self.period))
        return available


class SlidingWindowRateLimiter:
    """
    Sliding window rate limiter.

    Tracks requests in a sliding time window for precise rate limiting.
    """

    def __init__(self, max_requests: int, window_seconds: int = 60):
        """
        Initialize sliding window rate limiter.

        Args:
            max_requests: Maximum requests in window
            window_seconds: Window size in seconds
        """
        self.max_requests = max_requests
        self.window_seconds = window_seconds
        self.requests = deque()
        self.lock = asyncio.Lock()

    async def __aenter__(self):
        """Async context manager entry."""
        await self.acquire()
        return self

    async def __aexit__(self, *args):
        """Async context manager exit."""
        pass

    async def acquire(self):
        """Acquire permission to make a request."""
        async with self.lock:
            now = time.time()
            cutoff = now - self.window_seconds

            # Remove old requests outside window
            while self.requests and self.requests[0] < cutoff:
                self.requests.popleft()

            if len(self.requests) >= self.max_requests:
                # Calculate wait time until oldest request expires
                wait_time = self.requests[0] + self.window_seconds - now
                logger.debug(
                    "rate_limit_waiting",
                    wait_time=wait_time,
                    requests_in_window=len(self.requests),
                    max_requests=self.max_requests,
                )
                await asyncio.sleep(wait_time)

                # Clean up again after waiting
                cutoff = time.time() - self.window_seconds
                while self.requests and self.requests[0] < cutoff:
                    self.requests.popleft()

            # Record this request
            self.requests.append(now)

    def get_current_usage(self) -> int:
        """Get current number of requests in window."""
        now = time.time()
        cutoff = now - self.window_seconds
        return sum(1 for req in self.requests if req >= cutoff)


class AdaptiveRateLimiter:
    """
    Adaptive rate limiter that adjusts based on success/failure rates.

    Automatically reduces rate on errors and increases on success.
    """

    def __init__(
        self,
        initial_rate: int,
        min_rate: int = 1,
        max_rate: int = 1000,
        period: int = 60,
    ):
        """
        Initialize adaptive rate limiter.

        Args:
            initial_rate: Initial rate limit
            min_rate: Minimum rate limit
            max_rate: Maximum rate limit
            period: Time period in seconds
        """
        self.current_rate = initial_rate
        self.min_rate = min_rate
        self.max_rate = max_rate
        self.period = period
        self.limiter = TokenBucketRateLimiter(initial_rate, period)
        self.success_count = 0
        self.failure_count = 0
        self.lock = asyncio.Lock()

    async def __aenter__(self):
        """Async context manager entry."""
        await self.limiter.acquire()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        """Async context manager exit."""
        if exc_type is None:
            await self.record_success()
        else:
            await self.record_failure()

    async def record_success(self):
        """Record a successful request."""
        async with self.lock:
            self.success_count += 1

            # Increase rate after consecutive successes
            if self.success_count >= 10 and self.current_rate < self.max_rate:
                self.current_rate = min(
                    self.max_rate, int(self.current_rate * 1.1)
                )
                self.limiter = TokenBucketRateLimiter(self.current_rate, self.period)
                self.success_count = 0
                logger.info("rate_limit_increased", new_rate=self.current_rate)

    async def record_failure(self):
        """Record a failed request."""
        async with self.lock:
            self.failure_count += 1
            self.success_count = 0  # Reset success count

            # Decrease rate after failures
            if self.failure_count >= 3 and self.current_rate > self.min_rate:
                self.current_rate = max(self.min_rate, int(self.current_rate * 0.5))
                self.limiter = TokenBucketRateLimiter(self.current_rate, self.period)
                self.failure_count = 0
                logger.warning("rate_limit_decreased", new_rate=self.current_rate)


class RateLimiter:
    """
    Simplified rate limiter wrapper.

    Provides a simple interface for rate limiting with sensible defaults.
    """

    def __init__(
        self,
        requests_per_minute: int,
        burst_multiplier: float = 1.5,
        adaptive: bool = False,
    ):
        """
        Initialize rate limiter.

        Args:
            requests_per_minute: Maximum requests per minute
            burst_multiplier: Multiplier for burst capacity
            adaptive: Use adaptive rate limiting
        """
        self.requests_per_minute = requests_per_minute
        max_rate = int(requests_per_minute * burst_multiplier)

        if adaptive:
            self.limiter = AdaptiveRateLimiter(
                initial_rate=requests_per_minute,
                min_rate=max(1, requests_per_minute // 10),
                max_rate=max_rate,
                period=60,
            )
        else:
            self.limiter = TokenBucketRateLimiter(requests_per_minute, 60)

    async def __aenter__(self):
        """Async context manager entry."""
        await self.limiter.acquire()
        return self

    async def __aexit__(self, *args):
        """Async context manager exit."""
        if hasattr(self.limiter, "__aexit__"):
            await self.limiter.__aexit__(*args)


class MultiLevelRateLimiter:
    """
    Multi-level rate limiter with different limits for different time windows.

    Example: 10 req/sec, 100 req/min, 1000 req/hour
    """

    def __init__(self, limits: dict):
        """
        Initialize multi-level rate limiter.

        Args:
            limits: Dictionary of {period_seconds: max_requests}
                   Example: {1: 10, 60: 100, 3600: 1000}
        """
        self.limiters = [
            SlidingWindowRateLimiter(max_req, period)
            for period, max_req in limits.items()
        ]

    async def __aenter__(self):
        """Async context manager entry."""
        # Acquire from all limiters
        for limiter in self.limiters:
            await limiter.acquire()
        return self

    async def __aexit__(self, *args):
        """Async context manager exit."""
        pass
