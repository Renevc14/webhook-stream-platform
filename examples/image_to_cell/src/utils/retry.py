"""
Retry Utilities
===============

Decorators and utilities for retry logic with exponential backoff.
"""

import asyncio
import time
import logging
from functools import wraps
from typing import Callable, Tuple, Type, Optional, Any

from src.config.constants import RETRY_CONFIG


log = logging.getLogger(__name__)


class RetryError(Exception):
    """Raised when all retry attempts are exhausted."""

    def __init__(self, message: str, last_exception: Optional[Exception] = None):
        super().__init__(message)
        self.last_exception = last_exception


def is_retryable_error(error: Exception) -> bool:
    """
    Check if an error should trigger a retry.

    Args:
        error: The exception to check

    Returns:
        True if the error is retryable
    """
    error_str = str(error).lower()
    return any(
        retryable in error_str
        for retryable in RETRY_CONFIG.RETRYABLE_ERRORS
    )


def retry_on_failure(
    max_retries: int = 3,
    delay: float = 1.0,
    backoff: float = 2.0,
    exceptions: Tuple[Type[Exception], ...] = (Exception,),
    on_retry: Optional[Callable[[Exception, int], None]] = None,
):
    """
    Decorator to retry a function on failure with exponential backoff.

    Args:
        max_retries: Maximum number of retry attempts
        delay: Initial delay between retries in seconds
        backoff: Multiplier for delay after each retry
        exceptions: Tuple of exception types to catch and retry
        on_retry: Optional callback called on each retry (exception, attempt)

    Returns:
        Decorated function

    Example:
        @retry_on_failure(max_retries=3, delay=1.0)
        def fetch_data():
            ...
    """
    def decorator(func: Callable) -> Callable:
        @wraps(func)
        def wrapper(*args, **kwargs) -> Any:
            last_exception = None
            current_delay = delay

            for attempt in range(max_retries + 1):
                try:
                    return func(*args, **kwargs)
                except exceptions as e:
                    last_exception = e

                    if attempt == max_retries:
                        log.error(
                            f"Function {func.__name__} failed after {max_retries + 1} attempts: {e}"
                        )
                        raise RetryError(
                            f"Max retries ({max_retries}) exceeded for {func.__name__}",
                            last_exception=e
                        ) from e

                    log.warning(
                        f"Function {func.__name__} failed (attempt {attempt + 1}/{max_retries + 1}): {e}. "
                        f"Retrying in {current_delay:.1f}s..."
                    )

                    if on_retry:
                        on_retry(e, attempt)

                    time.sleep(current_delay)
                    current_delay *= backoff

            # Should never reach here, but just in case
            raise RetryError(
                f"Unexpected retry exhaustion for {func.__name__}",
                last_exception=last_exception
            )

        return wrapper
    return decorator


def retry_async(
    max_retries: int = 3,
    delay: float = 1.0,
    backoff: float = 2.0,
    exceptions: Tuple[Type[Exception], ...] = (Exception,),
    on_retry: Optional[Callable[[Exception, int], None]] = None,
):
    """
    Async version of retry decorator.

    Args:
        max_retries: Maximum number of retry attempts
        delay: Initial delay between retries in seconds
        backoff: Multiplier for delay after each retry
        exceptions: Tuple of exception types to catch and retry
        on_retry: Optional callback called on each retry (exception, attempt)

    Returns:
        Decorated async function

    Example:
        @retry_async(max_retries=3, delay=1.0)
        async def fetch_data():
            ...
    """
    def decorator(func: Callable) -> Callable:
        @wraps(func)
        async def wrapper(*args, **kwargs) -> Any:
            last_exception = None
            current_delay = delay

            for attempt in range(max_retries + 1):
                try:
                    return await func(*args, **kwargs)
                except exceptions as e:
                    last_exception = e

                    if attempt == max_retries:
                        log.error(
                            f"Async function {func.__name__} failed after {max_retries + 1} attempts: {e}"
                        )
                        raise RetryError(
                            f"Max retries ({max_retries}) exceeded for {func.__name__}",
                            last_exception=e
                        ) from e

                    log.warning(
                        f"Async function {func.__name__} failed (attempt {attempt + 1}/{max_retries + 1}): {e}. "
                        f"Retrying in {current_delay:.1f}s..."
                    )

                    if on_retry:
                        on_retry(e, attempt)

                    await asyncio.sleep(current_delay)
                    current_delay *= backoff

            raise RetryError(
                f"Unexpected retry exhaustion for {func.__name__}",
                last_exception=last_exception
            )

        return wrapper
    return decorator


class RetryContext:
    """
    Context manager for retry logic.

    Useful when you need more control over the retry process.

    Example:
        async with RetryContext(max_retries=3) as ctx:
            while ctx.should_retry():
                try:
                    result = await some_operation()
                    break
                except Exception as e:
                    await ctx.handle_error(e)
    """

    def __init__(
        self,
        max_retries: int = 3,
        delay: float = 1.0,
        backoff: float = 2.0,
        exceptions: Tuple[Type[Exception], ...] = (Exception,),
    ):
        self.max_retries = max_retries
        self.delay = delay
        self.backoff = backoff
        self.exceptions = exceptions
        self.attempt = 0
        self.current_delay = delay
        self.last_exception: Optional[Exception] = None

    def should_retry(self) -> bool:
        """Check if another retry attempt should be made."""
        return self.attempt <= self.max_retries

    async def handle_error(self, error: Exception) -> None:
        """
        Handle an error and prepare for retry.

        Args:
            error: The exception that occurred

        Raises:
            RetryError: If max retries exceeded or error not retryable
        """
        if not isinstance(error, self.exceptions):
            raise error

        self.last_exception = error
        self.attempt += 1

        if self.attempt > self.max_retries:
            raise RetryError(
                f"Max retries ({self.max_retries}) exceeded",
                last_exception=error
            ) from error

        log.warning(
            f"Attempt {self.attempt}/{self.max_retries + 1} failed: {error}. "
            f"Retrying in {self.current_delay:.1f}s..."
        )

        await asyncio.sleep(self.current_delay)
        self.current_delay *= self.backoff

    def handle_error_sync(self, error: Exception) -> None:
        """Synchronous version of handle_error."""
        if not isinstance(error, self.exceptions):
            raise error

        self.last_exception = error
        self.attempt += 1

        if self.attempt > self.max_retries:
            raise RetryError(
                f"Max retries ({self.max_retries}) exceeded",
                last_exception=error
            ) from error

        log.warning(
            f"Attempt {self.attempt}/{self.max_retries + 1} failed: {error}. "
            f"Retrying in {self.current_delay:.1f}s..."
        )

        time.sleep(self.current_delay)
        self.current_delay *= self.backoff

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        return False

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        return False
