"""
Rate Limiting Utilities
=======================

Thread-safe rate limiting for API calls.
"""

import asyncio
import time
import logging
from collections import deque
from threading import Lock, RLock
from typing import Optional, Tuple, Dict
from functools import wraps
from dataclasses import dataclass, field


log = logging.getLogger(__name__)


@dataclass
class RateLimitState:
    """State for rate limiting."""
    last_call: float = 0.0
    call_count: int = 0
    window_start: float = field(default_factory=time.time)


class RateLimiter:
    """
    Thread-safe rate limiter with sliding window.

    Supports both sync and async usage.

    Example:
        limiter = RateLimiter(max_calls=275, window_seconds=60)

        # Before each API call:
        limiter.acquire()  # Blocks if rate limit would be exceeded

        # Or check without blocking:
        allowed, wait_time = limiter.check()
        if not allowed:
            time.sleep(wait_time)
    """

    def __init__(
        self,
        max_calls: int = 275,
        window_seconds: int = 60,
        min_interval: float = 0.25,
    ):
        """
        Initialize rate limiter.

        Args:
            max_calls: Maximum calls allowed per window
            window_seconds: Window size in seconds
            min_interval: Minimum seconds between calls
        """
        self.max_calls = max(1, max_calls)
        self.window_seconds = max(1, window_seconds)
        self.min_interval = max(0.0, min_interval)

        self._lock = RLock()
        self._async_lock = asyncio.Lock()
        self._state = RateLimitState()
        self._call_times: deque = deque()

    def check(self) -> Tuple[bool, float]:
        """
        Check if a call is allowed without blocking.

        Returns:
            Tuple of (allowed, wait_time_seconds)
        """
        with self._lock:
            now = time.time()
            self._cleanup_old_calls(now)

            # Check min interval
            if self._state.last_call > 0:
                time_since_last = now - self._state.last_call
                if time_since_last < self.min_interval:
                    return False, self.min_interval - time_since_last

            # Check window limit
            if len(self._call_times) >= self.max_calls:
                oldest = self._call_times[0]
                wait_time = oldest + self.window_seconds - now
                if wait_time > 0:
                    return False, wait_time

            return True, 0.0

    def acquire(self) -> None:
        """
        Acquire permission to make a call, blocking if necessary.

        This should be called before each API call.
        """
        while True:
            with self._lock:
                allowed, wait_time = self.check()
                if allowed:
                    self._record_call()
                    return

            if wait_time > 0:
                log.debug(f"Rate limit: waiting {wait_time:.2f}s")
                time.sleep(wait_time)

    async def acquire_async(self) -> None:
        """Async version of acquire."""
        while True:
            async with self._async_lock:
                allowed, wait_time = self.check()
                if allowed:
                    self._record_call()
                    return

            if wait_time > 0:
                log.debug(f"Rate limit: waiting {wait_time:.2f}s")
                await asyncio.sleep(wait_time)

    def _record_call(self) -> None:
        """Record a call (must be called with lock held)."""
        now = time.time()
        self._state.last_call = now
        self._call_times.append(now)

    def _cleanup_old_calls(self, now: float) -> None:
        """Remove calls outside the window (must be called with lock held)."""
        cutoff = now - self.window_seconds
        while self._call_times and self._call_times[0] < cutoff:
            self._call_times.popleft()

    def get_stats(self) -> Dict:
        """Get current rate limiter statistics."""
        with self._lock:
            now = time.time()
            self._cleanup_old_calls(now)
            return {
                "calls_in_window": len(self._call_times),
                "max_calls": self.max_calls,
                "window_seconds": self.window_seconds,
                "min_interval": self.min_interval,
                "last_call_ago": now - self._state.last_call if self._state.last_call else None,
            }

    def reset(self) -> None:
        """Reset the rate limiter state."""
        with self._lock:
            self._state = RateLimitState()
            self._call_times.clear()


class PerKeyRateLimiter:
    """
    Rate limiter that tracks limits per key (e.g., per user).

    Example:
        limiter = PerKeyRateLimiter(max_calls=100, window_seconds=60)

        # Per-user rate limiting:
        allowed, wait_time = await limiter.check(f"user:{user_id}")
    """

    def __init__(
        self,
        max_calls: int = 100,
        window_seconds: int = 60,
    ):
        self.max_calls = max_calls
        self.window_seconds = window_seconds
        self._limiters: Dict[str, deque] = {}
        self._lock = asyncio.Lock()

    async def check(self, key: str) -> Tuple[bool, Optional[float]]:
        """
        Check if a call is allowed for the given key.

        Args:
            key: Unique identifier (e.g., user ID)

        Returns:
            Tuple of (allowed, retry_after_seconds or None)
        """
        async with self._lock:
            now = time.monotonic()

            # Get or create deque for this key
            if key not in self._limiters:
                self._limiters[key] = deque()

            events = self._limiters[key]

            # Remove old events
            while events and events[0] < now - self.window_seconds:
                events.popleft()

            # Check limit
            if len(events) >= self.max_calls:
                oldest = events[0]
                retry_after = oldest + self.window_seconds - now
                return False, max(0.0, retry_after)

            # Record this call
            events.append(now)
            return True, None

    async def cleanup(self) -> None:
        """Remove empty key entries to prevent memory growth."""
        async with self._lock:
            empty_keys = [
                key for key, events in self._limiters.items()
                if not events
            ]
            for key in empty_keys:
                del self._limiters[key]


def rate_limit(limiter: RateLimiter):
    """
    Decorator to apply rate limiting to a function.

    Args:
        limiter: RateLimiter instance to use

    Example:
        limiter = RateLimiter(max_calls=100)

        @rate_limit(limiter)
        def api_call():
            ...
    """
    def decorator(func):
        @wraps(func)
        def wrapper(*args, **kwargs):
            limiter.acquire()
            return func(*args, **kwargs)
        return wrapper
    return decorator


def rate_limit_async(limiter: RateLimiter):
    """Async version of rate_limit decorator."""
    def decorator(func):
        @wraps(func)
        async def wrapper(*args, **kwargs):
            await limiter.acquire_async()
            return await func(*args, **kwargs)
        return wrapper
    return decorator


# Global rate limiter for Smartsheet API
_smartsheet_limiter: Optional[RateLimiter] = None
_smartsheet_limiter_lock = Lock()


def get_smartsheet_limiter(
    max_calls: int = 275,
    min_interval: float = 0.25,
) -> RateLimiter:
    """
    Get the global Smartsheet API rate limiter.

    Args:
        max_calls: Max calls per minute
        min_interval: Min seconds between calls

    Returns:
        Singleton RateLimiter instance
    """
    global _smartsheet_limiter

    with _smartsheet_limiter_lock:
        if _smartsheet_limiter is None:
            _smartsheet_limiter = RateLimiter(
                max_calls=max_calls,
                window_seconds=60,
                min_interval=min_interval,
            )
        return _smartsheet_limiter
