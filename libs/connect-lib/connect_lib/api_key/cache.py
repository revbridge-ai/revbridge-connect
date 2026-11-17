"""
Cache for API Key validation

Supports:
- Memory (each process caches locally)
- Redis (shared by every instance)
- Tiered (in-memory L1 + fallback storage L2, for resilience); used when Redis is configured
"""

import json
import time
import hashlib
import asyncio
from abc import ABC, abstractmethod
from typing import Optional, Dict, Any
from collections import OrderedDict
import logging

logger = logging.getLogger(__name__)

# Default TTL: 5 minutes
DEFAULT_CACHE_TTL = 300


class ApiKeyCacheStorage(ABC):
    """Abstract interface for API Key cache backends"""

    @abstractmethod
    async def get(self, key: str) -> Optional[Dict[str, Any]]:
        """
        Reads data from the cache.

        Args:
            key: Cache key (hash of the API Key)

        Returns:
            Dict with the API Key's data, or None if not found/expired
        """
        pass

    @abstractmethod
    async def set(
        self, key: str, value: Dict[str, Any], ttl: int = DEFAULT_CACHE_TTL
    ) -> None:
        """
        Stores data in the cache.

        Args:
            key: Cache key
            value: Data to store
            ttl: Time to live in seconds
        """
        pass

    @abstractmethod
    async def delete(self, key: str) -> None:
        """Removes an entry from the cache"""
        pass

    @abstractmethod
    async def clear(self) -> None:
        """Clears the whole cache"""
        pass


class MemoryCacheStorage(ApiKeyCacheStorage):
    """
    Local in-memory LRU cache.

    Useful for development and tests. With several instances, each one keeps its own copy
    (an invalidation only reaches the local process).
    """

    def __init__(self, max_size: int = 1000):
        self._cache: OrderedDict[str, tuple] = OrderedDict()
        self._max_size = max_size
        self._lock = asyncio.Lock()

    async def get(self, key: str) -> Optional[Dict[str, Any]]:
        async with self._lock:
            if key not in self._cache:
                return None

            value, expires_at = self._cache[key]

            # Check the expiration
            if time.time() > expires_at:
                del self._cache[key]
                return None

            # Move to the end (LRU)
            self._cache.move_to_end(key)
            return value

    async def set(
        self, key: str, value: Dict[str, Any], ttl: int = DEFAULT_CACHE_TTL
    ) -> None:
        async with self._lock:
            expires_at = time.time() + ttl

            # Update it if it already exists
            if key in self._cache:
                self._cache[key] = (value, expires_at)
                self._cache.move_to_end(key)
                return

            # Keep memory bounded (LRU eviction)
            while len(self._cache) >= self._max_size:
                self._cache.popitem(last=False)

            self._cache[key] = (value, expires_at)

    async def delete(self, key: str) -> None:
        async with self._lock:
            if key in self._cache:
                del self._cache[key]

    async def clear(self) -> None:
        async with self._lock:
            self._cache.clear()


class RedisCacheStorage(ApiKeyCacheStorage):
    """
    Redis cache.

    Shared by every instance, so it works correctly with several instances.
    """

    def __init__(self, redis_client, key_prefix: str = "apikey_cache:"):
        """
        Initializes the Redis cache.

        Args:
            redis_client: Redis client (redis.asyncio.Redis or similar)
            key_prefix: Prefix for the Redis keys
        """
        self._redis = redis_client
        self._key_prefix = key_prefix

    def _make_key(self, key: str) -> str:
        """Adds the prefix to the key"""
        return f"{self._key_prefix}{key}"

    async def get(self, key: str) -> Optional[Dict[str, Any]]:
        try:
            data = await self._redis.get(self._make_key(key))
            if data is None:
                return None

            if isinstance(data, bytes):
                data = data.decode("utf-8")

            return json.loads(data)
        except Exception as e:
            logger.warning(f"Could not read the Redis cache: {e}")
            return None

    async def set(
        self, key: str, value: Dict[str, Any], ttl: int = DEFAULT_CACHE_TTL
    ) -> None:
        try:
            # Convert datetimes to strings if needed
            serializable_value = self._make_serializable(value)
            data = json.dumps(serializable_value)
            await self._redis.setex(self._make_key(key), ttl, data)
        except Exception as e:
            logger.warning(f"Could not write the Redis cache: {e}")

    async def delete(self, key: str) -> None:
        try:
            await self._redis.delete(self._make_key(key))
        except Exception as e:
            logger.warning(f"Could not delete from the Redis cache: {e}")

    async def clear(self) -> None:
        """Removes every key with the prefix"""
        try:
            pattern = f"{self._key_prefix}*"
            cursor = 0
            while True:
                cursor, keys = await self._redis.scan(cursor, match=pattern, count=100)
                if keys:
                    await self._redis.delete(*keys)
                if cursor == 0:
                    break
        except Exception as e:
            logger.warning(f"Could not clear the Redis cache: {e}")

    def _make_serializable(self, obj: Any) -> Any:
        """Converts objects to a JSON-serializable form"""
        from datetime import datetime, date

        if isinstance(obj, dict):
            return {k: self._make_serializable(v) for k, v in obj.items()}
        elif isinstance(obj, list):
            return [self._make_serializable(v) for v in obj]
        elif isinstance(obj, (datetime, date)):
            return obj.isoformat()
        elif hasattr(obj, "__dict__"):
            return self._make_serializable(obj.__dict__)
        return obj


class TieredCacheStorage(ApiKeyCacheStorage):
    """
    Two-layer cache: local memory (L1) + fallback storage (L2).

    L1 (in-memory): serves most requests with no network round-trip.
    L2 (fallback): only consulted on an L1 miss (a new process, a new key).

    Benefits:
    - Unaffected by Redis saturation (L1 does not depend on an external service)
    - Each process has its own L1
    - L2 can be Redis or any other ApiKeyCacheStorage
    - If L2 fails, only the first request after a restart is slow
    """

    def __init__(
        self,
        fallback: ApiKeyCacheStorage,
        max_memory_size: int = 1000,
    ):
        self._l1 = MemoryCacheStorage(max_size=max_memory_size)
        self._l2 = fallback

    async def get(self, key: str) -> Optional[Dict[str, Any]]:
        # L1: in-memory (fast path)
        result = await self._l1.get(key)
        if result is not None:
            return result

        # L2: fallback storage
        try:
            result = await self._l2.get(key)
            if result is not None:
                # Promote to L1
                await self._l1.set(key, result)
                return result
        except Exception as e:
            logger.warning(f"L2 cache fallback failed: {e}")

        return None

    async def set(
        self, key: str, value: Dict[str, Any], ttl: int = DEFAULT_CACHE_TTL
    ) -> None:
        # Always write to L1
        await self._l1.set(key, value, ttl)
        # Try to write to L2 (best effort)
        try:
            await self._l2.set(key, value, ttl)
        except Exception as e:
            logger.debug(f"L2 cache set failed (non-critical): {e}")

    async def delete(self, key: str) -> None:
        await self._l1.delete(key)
        try:
            await self._l2.delete(key)
        except Exception:
            pass

    async def clear(self) -> None:
        await self._l1.clear()
        try:
            await self._l2.clear()
        except Exception:
            pass


# Global cache instance
_api_key_cache: Optional[ApiKeyCacheStorage] = None
_cache_ttl: int = DEFAULT_CACHE_TTL
_cache_initialized: bool = False


def get_cache_key(api_key: str) -> str:
    """
    Builds the cache key of an API Key.
    Uses a SHA256 hash so the key itself is never exposed.
    """
    return hashlib.sha256(api_key.encode()).hexdigest()[:32]


async def _auto_init_cache() -> None:
    """
    Initializes the cache automatically from environment variables.
    Called automatically on the first cache operation.
    """
    global _cache_initialized

    if _cache_initialized:
        return

    _cache_initialized = True

    # Try to initialize with the Redis from the environment variables
    import os

    redis_url = os.getenv("REDIS_URL")
    redis_host = os.getenv("REDIS_HOST")
    redis_port = os.getenv("REDIS_PORT", "6379")
    redis_password = os.getenv("REDIS_PASSWORD", "")
    redis_db = os.getenv("REDIS_DB", "0")
    cache_ttl = int(os.getenv("API_KEY_CACHE_TTL", str(DEFAULT_CACHE_TTL)))

    if redis_url:
        await init_api_key_cache(redis_url=redis_url, ttl=cache_ttl)
    elif redis_host:
        if redis_password:
            url = f"redis://:{redis_password}@{redis_host}:{redis_port}/{redis_db}"
        else:
            url = f"redis://{redis_host}:{redis_port}/{redis_db}"
        await init_api_key_cache(redis_url=url, ttl=cache_ttl)
    else:
        # Fall back to memory
        await init_api_key_cache(use_memory=True, ttl=cache_ttl)


async def get_cached_api_key(api_key: str) -> Optional[Dict[str, Any]]:
    """
    Reads an API Key's data from the cache.

    Args:
        api_key: The full API Key

    Returns:
        Dict with the API Key's data, or None if not found
    """
    # Initialize the cache automatically if needed
    if _api_key_cache is None:
        await _auto_init_cache()

    if _api_key_cache is None:
        return None

    cache_key = get_cache_key(api_key)
    result = await _api_key_cache.get(cache_key)

    if result:
        logger.debug(f"API Key cache hit: {cache_key[:8]}...")

    return result


async def set_cached_api_key(
    api_key: str, data: Dict[str, Any], ttl: int = None
) -> None:
    """
    Stores an API Key's data in the cache.

    Args:
        api_key: The full API Key
        data: Data to store
        ttl: Time to live in seconds (optional; the default is used when not given)
    """
    # Initialize the cache automatically if needed
    if _api_key_cache is None:
        await _auto_init_cache()

    if _api_key_cache is None:
        return

    cache_key = get_cache_key(api_key)
    await _api_key_cache.set(cache_key, data, ttl or _cache_ttl)
    logger.debug(f"API Key cached: {cache_key[:8]}...")


async def invalidate_cached_api_key(api_key: str) -> None:
    """
    Removes an API Key from the cache.

    Args:
        api_key: The full API Key
    """
    if _api_key_cache is None:
        return

    cache_key = get_cache_key(api_key)
    await _api_key_cache.delete(cache_key)
    logger.debug(f"API Key cache invalidated: {cache_key[:8]}...")


async def clear_api_key_cache() -> None:
    """Clears the whole API Key cache"""
    if _api_key_cache is None:
        return

    await _api_key_cache.clear()
    logger.info("API Key cache cleared")


def set_api_key_cache(cache: ApiKeyCacheStorage, ttl: int = DEFAULT_CACHE_TTL) -> None:
    """
    Configures the API Key cache.

    Args:
        cache: Cache storage instance
        ttl: Default TTL in seconds
    """
    global _api_key_cache, _cache_ttl
    _api_key_cache = cache
    _cache_ttl = ttl
    logger.info(f"API Key cache configured with TTL={ttl}s")


def get_api_key_cache() -> Optional[ApiKeyCacheStorage]:
    """Returns the configured cache instance"""
    return _api_key_cache


async def init_api_key_cache(
    redis_url: Optional[str] = None,
    redis_client=None,
    use_memory: bool = False,
    ttl: int = DEFAULT_CACHE_TTL,
    max_memory_size: int = 1000,
) -> ApiKeyCacheStorage:
    """
    Initializes the API Key cache.

    Args:
        redis_url: Redis connection URL (e.g. redis://localhost:6379/0)
        redis_client: An already configured Redis client
        use_memory: If True, uses the in-memory cache (ignores Redis)
        ttl: Default TTL in seconds
        max_memory_size: Maximum size of the in-memory cache

    Returns:
        The configured ApiKeyCacheStorage instance
    """
    global _api_key_cache, _cache_ttl
    _cache_ttl = ttl

    if use_memory:
        logger.info(
            f"Using in-memory API Key cache (max_size={max_memory_size}, ttl={ttl}s)"
        )
        _api_key_cache = MemoryCacheStorage(max_size=max_memory_size)
        return _api_key_cache

    if redis_client:
        logger.info(f"Using tiered cache (memory L1 + Redis L2) for API Keys (ttl={ttl}s)")
        redis_storage = RedisCacheStorage(redis_client)
        _api_key_cache = TieredCacheStorage(fallback=redis_storage, max_memory_size=max_memory_size)
        return _api_key_cache

    if redis_url:
        try:
            from connect_lib.redis_client import create_async_client

            client = create_async_client(redis_url)
            # Test the connection
            await client.ping()
            logger.info(
                f"Using tiered cache (memory L1 + Redis L2) for API Keys (ttl={ttl}s)"
            )
            redis_storage = RedisCacheStorage(client)
            _api_key_cache = TieredCacheStorage(fallback=redis_storage, max_memory_size=max_memory_size)
            return _api_key_cache
        except Exception as e:
            logger.warning(
                f"Failed to connect to Redis: {e}. Using memory-only cache."
            )
            _api_key_cache = MemoryCacheStorage(max_size=max_memory_size)
            return _api_key_cache

    # Default: memory cache
    logger.info(f"No Redis configured. Using in-memory API Key cache (ttl={ttl}s)")
    _api_key_cache = MemoryCacheStorage(max_size=max_memory_size)
    return _api_key_cache
