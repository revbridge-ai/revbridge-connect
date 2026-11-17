import asyncio
import json
import logging
from typing import Any

from redis.exceptions import ConnectionError, RedisError, TimeoutError

from mail_api.common.config import REDIS_TTL_SEC, REDIS_URL

logger = logging.getLogger(__name__)


class AsyncRedisClient:
    """
    Async Redis client with retry logic and automatic reconnection.
    Uses the connect-lib factory for a client with hardened settings (keepalive, retry, health check).
    """

    def __init__(self, redis_url: str = None, max_connections: int = 50):
        self.redis_url = redis_url or REDIS_URL
        self.redis = None
        self.max_retries = 3
        self.retry_delay = 1
        self._connected = False
        self._max_connections = max_connections
        self._reconnect_lock = asyncio.Lock()

    async def _connect(self):
        """Connects to Redis using the central factory.

        Serialized by _reconnect_lock: several coroutines that notice a dead pool at
        the same time do NOT create competing ghost pools (avoids a reconnection storm).
        """
        async with self._reconnect_lock:
            # Another coroutine reconnected while we waited for the lock: stop here.
            if self.redis is not None and self._connected:
                return

            try:
                # Close the previous client, if any
                if self.redis is not None:
                    try:
                        await self.redis.aclose()
                    except Exception:
                        pass

                from connect_lib.redis_client import create_async_client

                self.redis = create_async_client(
                    self.redis_url,
                    max_connections=self._max_connections,
                )

                # Test the connection
                await self.redis.ping()
                self._connected = True
                logger.debug(f"Redis connected (max_connections={self._max_connections})")
            except Exception as e:
                logger.error(f"Error connecting to Redis: {e}")
                self.redis = None
                self._connected = False
                raise

    async def _ensure_connection(self):
        """Makes sure there is an active Redis connection.

        Note: no PING before each operation, to avoid doubling the Redis commands.
        If the connection fails, _execute_with_retry() reconnects automatically.
        """
        if self.redis is None or not self._connected:
            await self._connect()

    async def close(self):
        """Closes the Redis client and releases its resources."""
        self._connected = False
        try:
            if self.redis:
                await self.redis.aclose()
        except Exception:
            pass
        self.redis = None
        logger.info("Redis connection closed")

    async def _execute_with_retry(self, func):
        """Runs a Redis operation with retry.

        The pool is NEVER destroyed on an operation retry. The pool is a singleton
        shared by every FastAPI coroutine, so closing it because one operation failed
        would break all the other in-flight coroutines and cause a reconnection
        thundering herd.

        Only on ConnectionError is `_connected=False` set, so the next
        `_ensure_connection` recreates the pool, serialized by `_reconnect_lock`
        in `_connect()`.
        """
        last_exception = None

        for attempt in range(self.max_retries):
            try:
                await self._ensure_connection()
                if self.redis is None:
                    raise RuntimeError(
                        "Redis connection not available after ensure_connection"
                    )

                return await func()
            except (ConnectionError, TimeoutError, RedisError) as e:
                last_exception = e
                logger.warning(
                    f"Attempt {attempt + 1}/{self.max_retries} of a Redis operation failed: {e}"
                )

                if attempt < self.max_retries - 1:
                    await asyncio.sleep(self.retry_delay * (attempt + 1))
                    # Only flag a reconnect on ConnectionError (dead pool).
                    # TimeoutError/RedisError are operation-level: leave the pool alone.
                    if isinstance(e, ConnectionError):
                        self._connected = False
                else:
                    logger.error(f"All attempts failed. Last error: {e}")
            except RuntimeError as e:
                # Closed event loop: do not retry
                if "Event loop is closed" in str(e):
                    logger.warning(
                        "Event loop closed during a Redis operation. "
                        "This can happen during shutdown."
                    )
                    raise
                else:
                    logger.error(f"Unexpected error in a Redis operation: {e}")
                    raise
            except Exception as e:
                # Not a connection error: do not retry
                logger.error(f"Unexpected error in a Redis operation: {e}")
                raise

        # Getting here means every attempt failed
        raise last_exception

    async def setex(self, key: str, ttl: int, value: str):
        """Sets a key with a TTL."""

        async def _setex():
            return await self.redis.setex(key, ttl, value)

        return await self._execute_with_retry(_setex)

    async def set(self, key: str, value: str):
        """Sets a key without a TTL (kept until removed)."""

        async def _set():
            return await self.redis.set(key, value)

        return await self._execute_with_retry(_set)

    async def set_nx(self, key: str, value: str, ttl: int):
        """SETNX with a TTL: reserves the key only if it does not exist.

        Returns a truthy value when the key was reserved (the caller won), None when
        it already existed (the caller lost the race). Used for race-safe idempotency.
        """

        async def _set_nx():
            return await self.redis.set(key, value, nx=True, ex=ttl)

        return await self._execute_with_retry(_set_nx)

    async def get(self, key: str):
        """Returns the value of a key."""

        async def _get():
            return await self.redis.get(key)

        return await self._execute_with_retry(_get)

    async def keys(self, pattern: str):
        """Finds keys by pattern. DEPRECATED: use scan_keys() in production."""

        async def _keys():
            return await self.redis.keys(pattern)

        return await self._execute_with_retry(_keys)

    async def scan(self, cursor: int = 0, match: str = None, count: int = 100):
        """
        Finds keys with SCAN (non-blocking).

        Args:
            cursor: Starting cursor (0 to start)
            match: Search pattern (e.g. "e:*:*")
            count: Approximate number of keys per iteration

        Returns:
            tuple: (next_cursor, list_of_keys)
        """
        async def _scan():
            return await self.redis.scan(cursor, match=match, count=count)

        return await self._execute_with_retry(_scan)

    async def scan_keys(self, pattern: str, max_keys: int = None):
        """
        Finds every key matching the pattern with SCAN (non-blocking).

        Args:
            pattern: Search pattern (e.g. "e:*:*")
            max_keys: Maximum number of keys to return (None for all)

        Returns:
            list: Keys found
        """
        keys = []
        cursor = 0

        while True:
            cursor, batch = await self.scan(cursor=cursor, match=pattern, count=100)
            keys.extend(batch)

            # Cap the number of keys when requested
            if max_keys and len(keys) >= max_keys:
                return keys[:max_keys]

            # Cursor 0 means the iteration is over
            if cursor == 0:
                break

        return keys

    async def delete(self, *keys):
        """Deletes one or more keys."""

        async def _delete():
            return await self.redis.delete(*keys)

        return await self._execute_with_retry(_delete)

    async def ping(self):
        """Tests the connection."""

        async def _ping():
            return await self.redis.ping()

        return await self._execute_with_retry(_ping)

    async def llen(self, key: str):
        """Returns the length of the list."""

        async def _llen():
            return await self.redis.llen(key)

        return await self._execute_with_retry(_llen)

    async def lrange(self, key: str, start: int, end: int):
        """Returns a range of elements of the list."""

        async def _lrange():
            return await self.redis.lrange(key, start, end)

        return await self._execute_with_retry(_lrange)

    async def brpop(self, *keys, timeout: int = 0):
        """Removes and returns the last element of the list, blocking until one is available.

        Note: does not use _execute_with_retry because BRPOP blocks.
        If socket_timeout < the BRPOP timeout, redis-py raises TimeoutError,
        which is treated here as "no item" (returns None).
        """
        await self._ensure_connection()
        if self.redis is None:
            raise RuntimeError("Redis connection not available")

        try:
            return await self.redis.brpop(*keys, timeout=timeout)
        except TimeoutError:
            # Socket timeout while BRPOP was blocking: same as BRPOP returning nil
            return None
        except (ConnectionError, RedisError) as e:
            logger.warning(f"Connection error during BRPOP: {e}")
            self._connected = False
            raise

    async def ttl(self, key: str):
        """Returns the TTL of a key."""

        async def _ttl():
            return await self.redis.ttl(key)

        return await self._execute_with_retry(_ttl)

    async def key_type(self, key: str) -> str:
        """Returns the type of a key (string, list, set, zset, hash, none)."""

        async def _type():
            return await self.redis.type(key)

        return await self._execute_with_retry(_type)

    async def zrange(self, key: str, start: int, end: int, withscores: bool = False):
        """Returns a range of elements of the sorted set."""

        async def _zrange():
            return await self.redis.zrange(key, start, end, withscores=withscores)

        return await self._execute_with_retry(_zrange)

    async def zrevrange(self, key: str, start: int, end: int, withscores: bool = False):
        """Returns a range of elements of the sorted set in reverse order."""

        async def _zrevrange():
            return await self.redis.zrevrange(key, start, end, withscores=withscores)

        return await self._execute_with_retry(_zrevrange)

    async def info(self, section: str = None):
        """Returns Redis server information."""

        async def _info():
            return (
                await self.redis.info(section) if section else await self.redis.info()
            )

        return await self._execute_with_retry(_info)

    async def execute_command(self, command: str, *args):
        """Runs a generic Redis command."""

        async def _execute():
            return await self.redis.execute_command(command, *args)

        return await self._execute_with_retry(_execute)

    async def eval(self, script: str, num_keys: int, *keys_and_args):
        """Runs a Lua script in Redis."""

        async def _eval():
            return await self.redis.eval(script, num_keys, *keys_and_args)

        return await self._execute_with_retry(_eval)

    async def exists(self, key: str) -> bool:
        """Checks whether a key exists in Redis."""

        async def _exists():
            return bool(await self.redis.exists(key))

        return await self._execute_with_retry(_exists)

    async def ttl(self, key: str) -> int:
        """Returns the TTL of a key in Redis."""

        async def _ttl():
            return await self.redis.ttl(key)

        return await self._execute_with_retry(_ttl)

    async def zadd(self, key: str, mapping: dict[str, float]):
        """
        Adds one or more members to a sorted set, or updates their scores.

        Args:
            key: Name of the sorted set key
            mapping: Dict of {member: score}
        """
        async def _zadd():
            return await self.redis.zadd(key, mapping)

        return await self._execute_with_retry(_zadd)

    async def zrangebyscore(
        self,
        key: str,
        min: float | str = "-inf",
        max: float | str = "+inf",
        withscores: bool = False
    ):
        """
        Returns the members of a sorted set with scores within the given range.

        Args:
            key: Name of the sorted set key
            min: Minimum score ("-inf" for no lower bound)
            max: Maximum score ("+inf" for no upper bound)
            withscores: If True, returns (member, score) tuples
        """
        async def _zrangebyscore():
            return await self.redis.zrangebyscore(key, min, max, withscores=withscores)

        return await self._execute_with_retry(_zrangebyscore)

    async def zrem(self, key: str, *members):
        """
        Removes one or more members from a sorted set.

        Args:
            key: Name of the sorted set key
            *members: One or more members to remove
        """
        async def _zrem():
            return await self.redis.zrem(key, *members)

        return await self._execute_with_retry(_zrem)

    async def rpush(self, key: str, *values):
        """Appends one or more values to the end of the list (FIFO enqueue)."""

        async def _rpush():
            return await self.redis.rpush(key, *values)

        return await self._execute_with_retry(_rpush)

    async def lpush(self, key: str, *values):
        """Prepends one or more values to the start of the list."""

        async def _lpush():
            return await self.redis.lpush(key, *values)

        return await self._execute_with_retry(_lpush)

    async def rpop(self, key: str):
        """Removes and returns the last item of the list (the BRPOP end), or None."""

        async def _rpop():
            return await self.redis.rpop(key)

        return await self._execute_with_retry(_rpop)

    async def smembers(self, key: str):
        """Returns every member of a SET."""

        async def _smembers():
            return await self.redis.smembers(key)

        return await self._execute_with_retry(_smembers)

    async def sadd(self, key: str, *members):
        """Adds one or more members to a SET."""

        async def _sadd():
            return await self.redis.sadd(key, *members)

        return await self._execute_with_retry(_sadd)

    async def srem(self, key: str, *members):
        """Removes one or more members from a SET."""

        async def _srem():
            return await self.redis.srem(key, *members)

        return await self._execute_with_retry(_srem)

    async def expire(self, key: str, seconds: int):
        """Sets a TTL on an existing key."""

        async def _expire():
            return await self.redis.expire(key, seconds)

        return await self._execute_with_retry(_expire)

    async def incrby(self, key: str, amount: int = 1):
        """Increments an atomic counter (creates the key if it does not exist)."""

        async def _incrby():
            return await self.redis.incrby(key, amount)

        return await self._execute_with_retry(_incrby)


# Global instance
try:
    redis_client = AsyncRedisClient()
except Exception as e:
    logger.error(f"Critical failure initializing Redis: {e}")
    redis_client = None


def get_prefixed_key(
    key: str, queue_prefix: str = "w", domain_name: str | None = None
) -> str:
    """
    Builds a Redis key with the prefix pattern w:{domain_name}:*

    Args:
        key: Base key name (e.g. "queue", "retry")
        queue_prefix: Queue prefix (default: "w" for webhooks)
        domain_name: Domain name for isolation (optional)

    Returns:
        str: Key formatted as {queue_prefix}:{domain_name}:{key}
    """
    import os

    # Use the given domain_name, the environment variable or the default
    if not domain_name:
        domain_name = os.getenv("DOMAIN_NAME", "default")

    # Build the key as w:{domain_name}:{key}
    return f"{queue_prefix}:{domain_name}:{key}"


async def safe_setex(
    key: str,
    data: Any,
    ttl: int = None,
    redis_instance=None,
) -> bool:
    """
    Stores an item in Redis with a TTL using setex

    Args:
        key: Key name (may include suffixes such as event_type:message_id)
        data: Data to store (serialized to JSON when it is a dict/list)
        ttl: Time to live in seconds (default: REDIS_TTL_SEC from the config)
        redis_instance: Redis instance to use (default: redis_client)

    Returns:
        bool: True if the item was stored
    """
    if redis_instance is None:
        redis_instance = redis_client

    if redis_instance is None:
        logger.error(f"[REDIS_SETEX] Redis client not available for key: {key}")
        raise RuntimeError("Redis client not available")

    if ttl is None:
        ttl = REDIS_TTL_SEC

    try:
        # Serialize the data if needed
        if isinstance(data, str):
            json_data = data
        else:
            json_data = json.dumps(data, default=str)

        logger.debug(f"[REDIS_SETEX] Saving key: {key}, size: {len(json_data)} bytes, ttl: {ttl}s")

        # Use the class's setex (retry is built in)
        result = await redis_instance.setex(key, ttl, json_data)

        # Check whether the operation succeeded
        # redis.setex returns True on success
        # None or False means the operation failed
        if result is True:
            logger.debug(f"[REDIS_SETEX] Key saved: {key}")
            return True
        elif result is None:
            logger.error(f"[REDIS_SETEX] Failed to save the key in Redis (returned None): {key}")
            return False
        else:
            # Unexpected result (False or another value)
            logger.warning(f"[REDIS_SETEX] Unexpected result saving the key in Redis: {key}, result: {result}, type: {type(result)}")
            # Treat as success unless it is explicitly False
            return result is not False

    except Exception as e:
        logger.error(f"[REDIS_SETEX] Error saving the key in Redis: {key}, error: {e}", exc_info=True)
        raise
