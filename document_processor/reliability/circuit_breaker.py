"""
Circuit breaker implementation for fault tolerance.
Prevents cascading failures by breaking the circuit when errors exceed threshold.
"""

import asyncio
import time
from enum import Enum
from typing import Callable, Any, Optional
from ..config.logging_config import logger
from ..core.exceptions import CircuitBreakerOpenError


class CircuitState(Enum):
    """Circuit breaker states."""
    CLOSED = "closed"  # Normal operation
    OPEN = "open"  # Circuit broken, rejecting requests
    HALF_OPEN = "half_open"  # Testing if service recovered


class CircuitBreaker:
    """
    Circuit breaker pattern implementation.

    Monitors failures and opens circuit when threshold is exceeded,
    preventing requests to failing services.
    """

    def __init__(
        self,
        failure_threshold: int = 5,
        recovery_timeout: int = 60,
        expected_exception: type = Exception,
        half_open_max_calls: int = 3,
    ):
        """
        Initialize circuit breaker.

        Args:
            failure_threshold: Number of failures before opening circuit
            recovery_timeout: Seconds to wait before attempting recovery
            expected_exception: Exception type to count as failure
            half_open_max_calls: Max calls allowed in half-open state
        """
        self.failure_threshold = failure_threshold
        self.recovery_timeout = recovery_timeout
        self.expected_exception = expected_exception
        self.half_open_max_calls = half_open_max_calls

        self.failure_count = 0
        self.success_count = 0
        self.last_failure_time: Optional[float] = None
        self.state = CircuitState.CLOSED
        self.half_open_calls = 0
        self.lock = asyncio.Lock()

    async def call(self, func: Callable, *args, **kwargs) -> Any:
        """
        Execute function with circuit breaker protection.

        Args:
            func: Function to execute
            *args: Positional arguments
            **kwargs: Keyword arguments

        Returns:
            Function result

        Raises:
            CircuitBreakerOpenError: If circuit is open
            Exception: Original exception if circuit is closed
        """
        async with self.lock:
            # Check circuit state
            if self.state == CircuitState.OPEN:
                if time.time() - self.last_failure_time > self.recovery_timeout:
                    # Transition to half-open
                    self.state = CircuitState.HALF_OPEN
                    self.half_open_calls = 0
                    logger.info(
                        "circuit_breaker_half_open",
                        function=func.__name__,
                    )
                else:
                    # Still open, reject request
                    raise CircuitBreakerOpenError(
                        service=func.__name__,
                        message=f"Circuit breaker open for {func.__name__}",
                    )

            elif self.state == CircuitState.HALF_OPEN:
                if self.half_open_calls >= self.half_open_max_calls:
                    # Too many test calls, reject
                    raise CircuitBreakerOpenError(
                        service=func.__name__,
                        message=f"Circuit breaker half-open limit reached for {func.__name__}",
                    )
                self.half_open_calls += 1

        # Execute function
        try:
            if asyncio.iscoroutinefunction(func):
                result = await func(*args, **kwargs)
            else:
                result = func(*args, **kwargs)

            await self._on_success(func.__name__)
            return result

        except self.expected_exception as e:
            await self._on_failure(func.__name__)
            raise

    async def _on_success(self, func_name: str):
        """Handle successful call."""
        async with self.lock:
            self.failure_count = 0
            self.success_count += 1

            if self.state == CircuitState.HALF_OPEN:
                if self.success_count >= self.half_open_max_calls:
                    # Recovered, close circuit
                    self.state = CircuitState.CLOSED
                    self.half_open_calls = 0
                    logger.info(
                        "circuit_breaker_closed",
                        function=func_name,
                    )

    async def _on_failure(self, func_name: str):
        """Handle failed call."""
        async with self.lock:
            self.failure_count += 1
            self.success_count = 0
            self.last_failure_time = time.time()

            if self.state == CircuitState.HALF_OPEN:
                # Failed during recovery, reopen circuit
                self.state = CircuitState.OPEN
                logger.warning(
                    "circuit_breaker_reopened",
                    function=func_name,
                )

            elif self.failure_count >= self.failure_threshold:
                # Threshold exceeded, open circuit
                self.state = CircuitState.OPEN
                logger.error(
                    "circuit_breaker_opened",
                    function=func_name,
                    failure_count=self.failure_count,
                    threshold=self.failure_threshold,
                )

    def get_state(self) -> CircuitState:
        """Get current circuit state."""
        return self.state

    def get_stats(self) -> dict:
        """Get circuit breaker statistics."""
        return {
            "state": self.state.value,
            "failure_count": self.failure_count,
            "success_count": self.success_count,
            "failure_threshold": self.failure_threshold,
            "recovery_timeout": self.recovery_timeout,
        }

    async def reset(self):
        """Manually reset circuit breaker."""
        async with self.lock:
            self.failure_count = 0
            self.success_count = 0
            self.state = CircuitState.CLOSED
            self.half_open_calls = 0
            logger.info("circuit_breaker_reset")


class CircuitBreakerManager:
    """
    Manager for multiple circuit breakers.

    Maintains separate circuit breakers for different services.
    """

    def __init__(self):
        """Initialize circuit breaker manager."""
        self.breakers: dict[str, CircuitBreaker] = {}
        self.lock = asyncio.Lock()

    async def get_breaker(
        self,
        name: str,
        failure_threshold: int = 5,
        recovery_timeout: int = 60,
        expected_exception: type = Exception,
    ) -> CircuitBreaker:
        """
        Get or create circuit breaker for a service.

        Args:
            name: Service name
            failure_threshold: Failure threshold
            recovery_timeout: Recovery timeout
            expected_exception: Expected exception type

        Returns:
            Circuit breaker instance
        """
        async with self.lock:
            if name not in self.breakers:
                self.breakers[name] = CircuitBreaker(
                    failure_threshold=failure_threshold,
                    recovery_timeout=recovery_timeout,
                    expected_exception=expected_exception,
                )
            return self.breakers[name]

    async def call(self, service_name: str, func: Callable, *args, **kwargs) -> Any:
        """
        Execute function with circuit breaker protection.

        Args:
            service_name: Service name for circuit breaker
            func: Function to execute
            *args: Positional arguments
            **kwargs: Keyword arguments

        Returns:
            Function result
        """
        breaker = await self.get_breaker(service_name)
        return await breaker.call(func, *args, **kwargs)

    def get_all_stats(self) -> dict:
        """Get statistics for all circuit breakers."""
        return {name: breaker.get_stats() for name, breaker in self.breakers.items()}

    async def reset_all(self):
        """Reset all circuit breakers."""
        for breaker in self.breakers.values():
            await breaker.reset()
        logger.info("all_circuit_breakers_reset", count=len(self.breakers))
