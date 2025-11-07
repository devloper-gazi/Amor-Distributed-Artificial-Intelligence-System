"""
Retry logic with exponential backoff.
Provides decorators and functions for retrying failed operations.
"""

import asyncio
import random
from typing import Callable, Any, Optional, Type
from functools import wraps
from ..config.logging_config import logger
from ..core.exceptions import RetryExhaustedError


def calculate_backoff(
    attempt: int,
    base_delay: float = 1.0,
    max_delay: float = 60.0,
    exponential_base: float = 2.0,
    jitter: bool = True,
) -> float:
    """
    Calculate backoff delay with exponential backoff and jitter.

    Args:
        attempt: Attempt number (0-indexed)
        base_delay: Base delay in seconds
        max_delay: Maximum delay in seconds
        exponential_base: Base for exponential backoff
        jitter: Add random jitter to prevent thundering herd

    Returns:
        Delay in seconds
    """
    delay = min(base_delay * (exponential_base**attempt), max_delay)

    if jitter:
        # Add jitter: random value between 0 and delay
        delay = delay * (0.5 + random.random() * 0.5)

    return delay


async def async_retry(
    func: Callable,
    max_attempts: int = 3,
    base_delay: float = 1.0,
    max_delay: float = 60.0,
    exponential_base: float = 2.0,
    exceptions: tuple = (Exception,),
    jitter: bool = True,
    on_retry: Optional[Callable] = None,
) -> Any:
    """
    Retry async function with exponential backoff.

    Args:
        func: Async function to retry
        max_attempts: Maximum retry attempts
        base_delay: Base delay in seconds
        max_delay: Maximum delay in seconds
        exponential_base: Base for exponential backoff
        exceptions: Exceptions to catch and retry
        jitter: Add random jitter
        on_retry: Callback function called on each retry

    Returns:
        Function result

    Raises:
        RetryExhaustedError: If all retries exhausted
        Exception: Last exception if not in exceptions tuple
    """
    last_exception = None

    for attempt in range(max_attempts):
        try:
            return await func()
        except exceptions as e:
            last_exception = e

            if attempt < max_attempts - 1:
                delay = calculate_backoff(
                    attempt=attempt,
                    base_delay=base_delay,
                    max_delay=max_delay,
                    exponential_base=exponential_base,
                    jitter=jitter,
                )

                logger.warning(
                    "retry_attempt",
                    function=func.__name__,
                    attempt=attempt + 1,
                    max_attempts=max_attempts,
                    delay=delay,
                    error=str(e),
                )

                if on_retry:
                    await on_retry(attempt, e)

                await asyncio.sleep(delay)
            else:
                logger.error(
                    "retry_exhausted",
                    function=func.__name__,
                    attempts=max_attempts,
                    error=str(e),
                )

    raise RetryExhaustedError(
        f"Failed after {max_attempts} attempts: {last_exception}",
        attempts=max_attempts,
    )


def retry_decorator(
    max_attempts: int = 3,
    base_delay: float = 1.0,
    max_delay: float = 60.0,
    exponential_base: float = 2.0,
    exceptions: tuple = (Exception,),
    jitter: bool = True,
):
    """
    Decorator for retrying async functions.

    Args:
        max_attempts: Maximum retry attempts
        base_delay: Base delay in seconds
        max_delay: Maximum delay in seconds
        exponential_base: Base for exponential backoff
        exceptions: Exceptions to catch and retry
        jitter: Add random jitter

    Returns:
        Decorated function
    """

    def decorator(func: Callable) -> Callable:
        @wraps(func)
        async def wrapper(*args, **kwargs):
            return await async_retry(
                lambda: func(*args, **kwargs),
                max_attempts=max_attempts,
                base_delay=base_delay,
                max_delay=max_delay,
                exponential_base=exponential_base,
                exceptions=exceptions,
                jitter=jitter,
            )

        return wrapper

    return decorator


class RetryPolicy:
    """
    Configurable retry policy.

    Encapsulates retry configuration and logic.
    """

    def __init__(
        self,
        max_attempts: int = 3,
        base_delay: float = 1.0,
        max_delay: float = 60.0,
        exponential_base: float = 2.0,
        exceptions: tuple = (Exception,),
        jitter: bool = True,
        timeout: Optional[float] = None,
    ):
        """
        Initialize retry policy.

        Args:
            max_attempts: Maximum retry attempts
            base_delay: Base delay in seconds
            max_delay: Maximum delay in seconds
            exponential_base: Base for exponential backoff
            exceptions: Exceptions to catch and retry
            jitter: Add random jitter
            timeout: Overall timeout in seconds
        """
        self.max_attempts = max_attempts
        self.base_delay = base_delay
        self.max_delay = max_delay
        self.exponential_base = exponential_base
        self.exceptions = exceptions
        self.jitter = jitter
        self.timeout = timeout

    async def execute(self, func: Callable, *args, **kwargs) -> Any:
        """
        Execute function with retry policy.

        Args:
            func: Function to execute
            *args: Positional arguments
            **kwargs: Keyword arguments

        Returns:
            Function result
        """
        if self.timeout:
            return await asyncio.wait_for(
                async_retry(
                    lambda: func(*args, **kwargs),
                    max_attempts=self.max_attempts,
                    base_delay=self.base_delay,
                    max_delay=self.max_delay,
                    exponential_base=self.exponential_base,
                    exceptions=self.exceptions,
                    jitter=self.jitter,
                ),
                timeout=self.timeout,
            )
        else:
            return await async_retry(
                lambda: func(*args, **kwargs),
                max_attempts=self.max_attempts,
                base_delay=self.base_delay,
                max_delay=self.max_delay,
                exponential_base=self.exponential_base,
                exceptions=self.exceptions,
                jitter=self.jitter,
            )

    def decorator(self) -> Callable:
        """Get decorator for this retry policy."""
        return retry_decorator(
            max_attempts=self.max_attempts,
            base_delay=self.base_delay,
            max_delay=self.max_delay,
            exponential_base=self.exponential_base,
            exceptions=self.exceptions,
            jitter=self.jitter,
        )


# Predefined retry policies
AGGRESSIVE_RETRY = RetryPolicy(
    max_attempts=5,
    base_delay=0.5,
    max_delay=30.0,
    exponential_base=2.0,
    jitter=True,
)

STANDARD_RETRY = RetryPolicy(
    max_attempts=3,
    base_delay=1.0,
    max_delay=60.0,
    exponential_base=2.0,
    jitter=True,
)

CONSERVATIVE_RETRY = RetryPolicy(
    max_attempts=2,
    base_delay=2.0,
    max_delay=120.0,
    exponential_base=3.0,
    jitter=True,
)

NETWORK_RETRY = RetryPolicy(
    max_attempts=4,
    base_delay=1.0,
    max_delay=30.0,
    exponential_base=2.0,
    jitter=True,
    timeout=60.0,
)
