import logging
import time

from redis.exceptions import ConnectionError, RedisError, TimeoutError

from mail_milter.common.config import REDIS_URL

logger = logging.getLogger(__name__)


class RedisClient:
    """
    Redis client with retry logic and automatic reconnection.
    """

    def __init__(self, redis_url: str = None):
        self.redis_url = redis_url or REDIS_URL
        self.redis = None
        self.max_retries = 3
        self.retry_delay = 1
        self._connect()

    def _connect(self):
        """Connect to Redis through the shared client factory."""
        try:
            from connect_lib.redis_client import create_sync_client

            self.redis = create_sync_client(self.redis_url)
            # Test the connection
            self.redis.ping()
            logger.info("Redis connection established")
        except Exception as e:
            logger.error(f"Failed to connect to Redis: {e}")
            self.redis = None
            raise

    def _ensure_connection(self):
        """Make sure there is an active Redis connection.

        Does not ping: in large batches (e.g. events_updater /postfix), a ping
        before every operation would double the number of Redis round-trips.
        The lazy reconnection in _execute_with_retry already covers a dead
        connection: the real operation fails, is retried, closes the dead socket
        and reconnects.
        """
        if self.redis is None:
            self._connect()
            if self.redis is None:
                raise ConnectionError("Failed to establish Redis connection")

    def _execute_with_retry(self, method_name: str, *args, **kwargs):
        """Run a Redis operation with retries."""
        last_exception = None

        for attempt in range(self.max_retries):
            try:
                self._ensure_connection()
                # Check whether self.redis is still None after _ensure_connection
                if self.redis is None:
                    raise ConnectionError("Redis connection is None after ensure_connection")
                # Look up the method dynamically once the connection is ensured
                if not hasattr(self.redis, method_name):
                    raise AttributeError(f"Redis client does not have method '{method_name}'")
                method = getattr(self.redis, method_name)
                return method(*args, **kwargs)
            except (ConnectionError, TimeoutError, RedisError) as e:
                last_exception = e
                logger.warning(
                    f"Attempt {attempt + 1}/{self.max_retries} of a Redis operation failed: {e}"
                )

                if attempt < self.max_retries - 1:
                    time.sleep(self.retry_delay * (attempt + 1))
                    # Reconnect
                    try:
                        if self.redis:
                            self.redis.close()
                    except:
                        pass
                    self.redis = None
                else:
                    logger.error(f"All attempts failed. Last error: {e}")
            except Exception as e:
                # Not a connection error: do not retry
                logger.error(f"Unexpected error in Redis operation: {e}")
                raise

        # All attempts failed
        raise last_exception

    def setex(self, key: str, ttl: int, value: str):
        """Set a key with a TTL."""
        return self._execute_with_retry("setex", key, ttl, value)

    def set(self, key: str, value: str):
        """Set a key without a TTL (kept until removed)."""
        return self._execute_with_retry("set", key, value)

    def set_if_absent(self, key: str, ttl: int, value: str):
        """SET NX + EX: write the key with a TTL only if it does not exist.

        Returns True if written, None if the key already existed.
        """
        return self._execute_with_retry("set", key, value, nx=True, ex=ttl)

    def get(self, key: str):
        """Get the value of a key."""
        return self._execute_with_retry("get", key)

    def mget(self, keys):
        """
        Get several values at once (1 round-trip).

        Args:
            keys: List of keys
        Returns:
            List of values in the same order (None for missing keys)
        """
        return self._execute_with_retry("mget", list(keys))

    def keys(self, pattern: str):
        """Find keys by pattern. DEPRECATED: use scan() in production."""
        return self._execute_with_retry("keys", pattern)

    def scan(self, cursor: int = 0, match: str = None, count: int = 100):
        """
        Find keys with SCAN (non-blocking).

        Args:
            cursor: Starting cursor (0 to start)
            match: Match pattern (e.g. "q:*")
            count: Approximate number of keys per iteration

        Returns:
            tuple: (next_cursor, list_of_keys)
        """
        return self._execute_with_retry("scan", cursor, match=match, count=count)

    def scan_keys(self, pattern: str, max_keys: int = None):
        """
        Find all keys matching the pattern with SCAN (non-blocking).

        Args:
            pattern: Match pattern (e.g. "q:*")
            max_keys: Maximum number of keys to return (None for all)

        Returns:
            list: Keys found
        """
        keys = []
        cursor = 0

        while True:
            cursor, batch = self.scan(cursor=cursor, match=pattern, count=100)
            keys.extend(batch)

            # Stop at max_keys, if given
            if max_keys and len(keys) >= max_keys:
                return keys[:max_keys]

            # Cursor 0 means the iteration is complete
            if cursor == 0:
                break

        return keys

    def lpush(self, key: str, value: str):
        """Push a value to the head of a list."""
        return self._execute_with_retry("lpush", key, value)

    def sismember(self, key: str, member: str):
        """Set membership (used by the warmup divisor's SPF gate).

        The divisor loader depends on it: without it the lookup would raise,
        spf_ok would be False and every eligible destination would be reported
        as spf_not_authorized.
        """
        return self._execute_with_retry("sismember", key, member)

    def incr(self, key: str):
        """Atomic increment (the warmup divisor's daily cap counter)."""
        return self._execute_with_retry("incr", key)

    def expire(self, key: str, ttl: int):
        """Set a TTL (expiry of the warmup divisor's daily counter)."""
        return self._execute_with_retry("expire", key, ttl)

    def ping(self):
        """Test the connection."""
        return self._execute_with_retry("ping")


# Global instance
try:
    redis_client = RedisClient()
    # Expose the client interface directly, for compatibility
    redis = redis_client
except Exception as e:
    logger.error(f"Critical failure initializing Redis: {e}")
    # No client: callers that need Redis fail explicitly
    redis = None
