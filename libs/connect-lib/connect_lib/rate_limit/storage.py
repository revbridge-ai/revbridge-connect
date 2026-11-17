"""
Storage Backends for Rate Limiting

Supports:
- Redis (distributed, shared by every instance)
- Memory (for development/tests)
"""

import time
import asyncio
from abc import ABC, abstractmethod
from typing import Optional, Dict, List
from collections import defaultdict
import logging

logger = logging.getLogger(__name__)


class RateLimitStorage(ABC):
    """Abstract interface for storage backends"""

    @abstractmethod
    async def increment(
        self,
        key: str,
        window_start: float,
        window_size: int,
        timestamp: float,
    ) -> int:
        """
        Records a new request and returns the count in the window.

        Args:
            key: Identifying key
            window_start: Start of the time window
            window_size: Window size in seconds
            timestamp: Timestamp of the current request

        Returns:
            Number of requests in the current window
        """
        pass

    @abstractmethod
    async def reset(self, key: str) -> None:
        """Removes every record of a key"""
        pass

    @abstractmethod
    async def get_count(self, key: str, window_start: float) -> int:
        """Returns the current count of a key"""
        pass


class MemoryStorage(RateLimitStorage):
    """
    Local in-memory storage.

    Useful for development and tests, but it does not work across several instances.
    """

    def __init__(self, cleanup_interval: int = 60):
        self._storage: Dict[str, List[float]] = defaultdict(list)
        self._lock = asyncio.Lock()
        self._cleanup_interval = cleanup_interval
        self._last_cleanup = time.time()

    async def increment(
        self,
        key: str,
        window_start: float,
        window_size: int,
        timestamp: float,
    ) -> int:
        async with self._lock:
            # Periodic cleanup
            await self._maybe_cleanup(window_start)

            # Drop the old requests and add the new one
            self._storage[key] = [ts for ts in self._storage[key] if ts >= window_start]
            self._storage[key].append(timestamp)

            return len(self._storage[key])

    async def reset(self, key: str) -> None:
        async with self._lock:
            if key in self._storage:
                del self._storage[key]

    async def get_count(self, key: str, window_start: float) -> int:
        async with self._lock:
            if key not in self._storage:
                return 0
            return len([ts for ts in self._storage[key] if ts >= window_start])

    async def _maybe_cleanup(self, window_start: float) -> None:
        """Removes expired entries periodically"""
        now = time.time()
        if now - self._last_cleanup < self._cleanup_interval:
            return

        keys_to_remove = []
        for key, timestamps in self._storage.items():
            self._storage[key] = [ts for ts in timestamps if ts >= window_start]
            if not self._storage[key]:
                keys_to_remove.append(key)

        for key in keys_to_remove:
            del self._storage[key]

        self._last_cleanup = now


class RedisStorage(RateLimitStorage):
    """
    Redis storage with Sorted Sets.

    Shared by every instance, so it works correctly with several instances.

    Fail-open behavior: when Redis is unavailable, requests are allowed (count=0 is
    returned) instead of blocking traffic.
    """

    def __init__(self, redis_client, fail_open: bool = True):
        """
        Initializes the Redis storage.

        Args:
            redis_client: Redis client (redis.asyncio.Redis or similar)
            fail_open: If True, allows requests when Redis fails.
                       If False, raises an exception.
        """
        self._redis = redis_client
        self._fail_open = fail_open
        self._last_error_logged = 0.0

    async def increment(
        self,
        key: str,
        window_start: float,
        window_size: int,
        timestamp: float,
    ) -> int:
        """
        Uses a Sorted Set to implement the sliding window.

        The score is the timestamp and the member is the timestamp + a unique suffix.
        """
        try:
            pipe = self._redis.pipeline()

            # Remove expired entries (outside the window)
            pipe.zremrangebyscore(key, "-inf", window_start)

            # Add the new entry with score = timestamp
            # The member uses the timestamp with enough precision to be unique
            member = f"{timestamp}:{id(timestamp)}"
            pipe.zadd(key, {member: timestamp})

            # Count the entries in the window
            pipe.zcard(key)

            # TTL for automatic cleanup
            pipe.expire(key, window_size + 10)

            results = await pipe.execute()

            # The third result is the count
            count = results[2]
            return count
        except Exception as e:
            return self._handle_error("increment", e)

    async def reset(self, key: str) -> None:
        """Removes the key from Redis"""
        try:
            await self._redis.delete(key)
        except Exception as e:
            self._handle_error("reset", e)

    async def get_count(self, key: str, window_start: float) -> int:
        """Returns the number of requests in the window"""
        try:
            # First remove the expired entries
            await self._redis.zremrangebyscore(key, "-inf", window_start)
            # Then count
            return await self._redis.zcard(key)
        except Exception as e:
            return self._handle_error("get_count", e)

    def _handle_error(self, operation: str, error: Exception) -> int:
        """
        Handles Redis connection errors.

        Logs the error (rate limited, so the logs are not flooded) and returns 0 if
        fail_open is enabled; otherwise re-raises the exception.

        Stale-connection errors (a TCPTransport closed by the server while idle, e.g. a
        managed Redis recycling the connection) are logged as WARNING instead of ERROR:
        fail-open means the user is not affected, and the pool is forced to drop the dead
        connection so the next operation gets a fresh one.
        """
        err_msg = str(error)
        is_stale_conn = (
            "TCPTransport closed" in err_msg
            or "handler is closed" in err_msg
            or "Connection closed by server" in err_msg
        )

        if is_stale_conn:
            # Drop EVERY connection of the pool - the next operation forces a reconnect.
            # This keeps the pool from serving the same dead connection over and over.
            try:
                pool = getattr(self._redis, "connection_pool", None)
                if pool is not None:
                    asyncio.create_task(pool.disconnect(inuse_connections=False))
            except Exception:
                pass

        # Rate-limited logging to avoid flooding (at most 1 log per second)
        now = time.time()
        if now - self._last_error_logged >= 1.0:
            log_fn = logger.warning if is_stale_conn else logger.error
            log_fn(
                f"Error in the rate limiting middleware: {error}",
                extra={"operation": operation, "error_type": type(error).__name__},
            )
            self._last_error_logged = now

        if self._fail_open:
            # Fail open: return 0 to allow the request
            return 0
        raise error


# Storage factory
_default_storage: Optional[RateLimitStorage] = None


async def get_storage(
    redis_url: Optional[str] = None,
    redis_client=None,
    use_memory: bool = False,
) -> RateLimitStorage:
    """
    Factory that returns a storage instance.

    Args:
        redis_url: Redis connection URL (e.g. redis://localhost:6379/0)
        redis_client: An already configured Redis client
        use_memory: If True, uses in-memory storage (ignores Redis)

    Returns:
        RateLimitStorage instance
    """
    if use_memory:
        logger.info("Using in-memory rate limit storage")
        return MemoryStorage()

    if redis_client:
        logger.info("Using provided Redis client for rate limit storage")
        return RedisStorage(redis_client)

    if redis_url:
        try:
            from connect_lib.redis_client import create_async_client

            client = create_async_client(redis_url)
            logger.info("Connected to Redis for rate limiting")
            return RedisStorage(client)
        except Exception as e:
            logger.warning(
                f"Failed to connect to Redis: {e}. Falling back to memory storage."
            )
            return MemoryStorage()

    # Default: memory storage
    logger.info("No Redis configured. Using in-memory rate limit storage.")
    return MemoryStorage()


def get_default_storage() -> Optional[RateLimitStorage]:
    """Returns the global default storage"""
    return _default_storage
