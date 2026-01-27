"""
Error handling with dead letter queue support.
Captures failed messages and routes them for later processing.
"""

import asyncio
import json
from typing import Any, Optional, Callable, Type
from datetime import datetime
from ..config.logging_config import logger
from ..core.models import SourceDocument, ProcessingStatus
from ..core.exceptions import DocumentProcessorException


class DeadLetterQueue:
    """
    Dead letter queue for failed messages.

    Stores failed messages for later retry or manual processing.
    """

    def __init__(self, max_retries: int = 3):
        """
        Initialize dead letter queue.

        Args:
            max_retries: Maximum retry attempts before final failure
        """
        self.max_retries = max_retries
        self.messages = []
        self.lock = asyncio.Lock()

    async def add_message(
        self,
        message: Any,
        error: Exception,
        retry_count: int = 0,
        metadata: dict = None,
    ):
        """
        Add failed message to DLQ.

        Args:
            message: Original message that failed
            error: Exception that occurred
            retry_count: Number of retry attempts
            metadata: Additional metadata
        """
        async with self.lock:
            dlq_entry = {
                "message": message,
                "error": str(error),
                "error_type": type(error).__name__,
                "retry_count": retry_count,
                "metadata": metadata or {},
                "timestamp": datetime.utcnow().isoformat(),
            }

            self.messages.append(dlq_entry)

            logger.error(
                "message_added_to_dlq",
                error=str(error),
                error_type=type(error).__name__,
                retry_count=retry_count,
                message_id=getattr(message, "id", "unknown"),
            )

    async def get_messages(self, limit: Optional[int] = None) -> list:
        """
        Get messages from DLQ.

        Args:
            limit: Maximum number of messages to return

        Returns:
            List of DLQ messages
        """
        async with self.lock:
            if limit:
                return self.messages[:limit]
            return self.messages.copy()

    async def remove_message(self, index: int):
        """
        Remove message from DLQ.

        Args:
            index: Index of message to remove
        """
        async with self.lock:
            if 0 <= index < len(self.messages):
                self.messages.pop(index)

    async def clear(self):
        """Clear all messages from DLQ."""
        async with self.lock:
            count = len(self.messages)
            self.messages.clear()
            logger.info("dlq_cleared", message_count=count)

    async def get_stats(self) -> dict:
        """Get DLQ statistics."""
        async with self.lock:
            error_types = {}
            for msg in self.messages:
                error_type = msg["error_type"]
                error_types[error_type] = error_types.get(error_type, 0) + 1

            return {
                "total_messages": len(self.messages),
                "error_types": error_types,
                "max_retries": self.max_retries,
            }


class ErrorHandler:
    """
    Comprehensive error handler with DLQ support.

    Handles errors, logging, retries, and dead letter queueing.
    """

    def __init__(
        self,
        dlq: Optional[DeadLetterQueue] = None,
        max_retries: int = 3,
        error_callbacks: dict = None,
    ):
        """
        Initialize error handler.

        Args:
            dlq: Dead letter queue instance
            max_retries: Maximum retry attempts
            error_callbacks: Dictionary of error type -> callback function
        """
        self.dlq = dlq or DeadLetterQueue(max_retries=max_retries)
        self.max_retries = max_retries
        self.error_callbacks = error_callbacks or {}
        self.error_counts = {}
        self.lock = asyncio.Lock()

    async def handle_error(
        self,
        error: Exception,
        message: Any = None,
        retry_count: int = 0,
        metadata: dict = None,
    ) -> bool:
        """
        Handle error with logging, callbacks, and DLQ.

        Args:
            error: Exception that occurred
            message: Original message
            retry_count: Current retry count
            metadata: Additional metadata

        Returns:
            True if should retry, False if should send to DLQ
        """
        error_type = type(error).__name__

        # Update error counts
        async with self.lock:
            self.error_counts[error_type] = self.error_counts.get(error_type, 0) + 1

        # Log error
        logger.error(
            "error_occurred",
            error=str(error),
            error_type=error_type,
            retry_count=retry_count,
            message_id=getattr(message, "id", "unknown"),
            metadata=metadata,
            exc_info=True,
        )

        # Execute error callback if exists
        if error_type in self.error_callbacks:
            try:
                callback = self.error_callbacks[error_type]
                if asyncio.iscoroutinefunction(callback):
                    await callback(error, message, retry_count)
                else:
                    callback(error, message, retry_count)
            except Exception as callback_error:
                logger.error(
                    "error_callback_failed",
                    error=str(callback_error),
                    original_error=str(error),
                )

        # Determine if should retry
        should_retry = retry_count < self.max_retries

        if not should_retry:
            # Add to DLQ
            await self.dlq.add_message(
                message=message,
                error=error,
                retry_count=retry_count,
                metadata=metadata,
            )

        return should_retry

    async def execute_with_error_handling(
        self,
        func: Callable,
        *args,
        message: Any = None,
        retry_count: int = 0,
        metadata: dict = None,
        **kwargs,
    ) -> Any:
        """
        Execute function with error handling.

        Args:
            func: Function to execute
            *args: Positional arguments
            message: Message being processed
            retry_count: Current retry count
            metadata: Additional metadata
            **kwargs: Keyword arguments

        Returns:
            Function result

        Raises:
            Exception: If error handling determines not to suppress
        """
        try:
            if asyncio.iscoroutinefunction(func):
                return await func(*args, **kwargs)
            else:
                return func(*args, **kwargs)
        except Exception as e:
            should_retry = await self.handle_error(
                error=e,
                message=message,
                retry_count=retry_count,
                metadata=metadata,
            )

            if should_retry:
                raise  # Re-raise for retry logic
            else:
                # Sent to DLQ, don't re-raise
                logger.info(
                    "error_sent_to_dlq",
                    message_id=getattr(message, "id", "unknown"),
                )
                return None

    def register_error_callback(self, error_type: Type[Exception], callback: Callable):
        """
        Register callback for specific error type.

        Args:
            error_type: Exception class
            callback: Callback function
        """
        self.error_callbacks[error_type.__name__] = callback
        logger.info(
            "error_callback_registered",
            error_type=error_type.__name__,
        )

    async def get_error_stats(self) -> dict:
        """Get error statistics."""
        async with self.lock:
            dlq_stats = await self.dlq.get_stats()
            return {
                "error_counts": self.error_counts.copy(),
                "dlq": dlq_stats,
            }

    async def retry_dlq_messages(
        self,
        processor: Callable,
        limit: Optional[int] = None,
    ) -> dict:
        """
        Retry messages from DLQ.

        Args:
            processor: Function to process messages
            limit: Maximum messages to retry

        Returns:
            Statistics about retry operation
        """
        messages = await self.dlq.get_messages(limit=limit)

        success_count = 0
        failure_count = 0

        for i, dlq_entry in enumerate(messages):
            try:
                message = dlq_entry["message"]

                if asyncio.iscoroutinefunction(processor):
                    await processor(message)
                else:
                    processor(message)

                # Success - remove from DLQ
                await self.dlq.remove_message(i - failure_count)
                success_count += 1

                logger.info(
                    "dlq_message_retried_successfully",
                    message_id=getattr(message, "id", "unknown"),
                )

            except Exception as e:
                failure_count += 1
                logger.error(
                    "dlq_message_retry_failed",
                    error=str(e),
                    message_id=getattr(dlq_entry.get("message"), "id", "unknown"),
                )

        return {
            "attempted": len(messages),
            "successful": success_count,
            "failed": failure_count,
        }
