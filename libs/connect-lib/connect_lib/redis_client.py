"""
Centralized Redis client factory for RevBridge services.

Provides hardened Redis clients with:
- TCP keepalive, so idle connections are not dropped by load balancers or NAT gateways
- Health check interval to detect stale/dead connections before use
- Exponential backoff retry on transient connection errors
- Connection pool limits to prevent exhaustion
- Proper socket timeouts

Usage (async - FastAPI services):

    from connect_lib.redis_client import create_async_client

    redis = create_async_client("redis://localhost:6379/0")
    await redis.ping()

Usage (sync - workers with BLPOP):

    from connect_lib.redis_client import create_sync_client

    redis = create_sync_client(
        "redis://localhost:6379/0",
        socket_timeout=30.0,  # Must be > BLPOP timeout
    )
"""

import logging
from typing import List, Type

logger = logging.getLogger(__name__)

# Default connection parameters (keepalive + health checks for long-lived connections)
DEFAULTS = {
    "encoding": "utf-8",
    "decode_responses": True,
    "socket_timeout": 5.0,
    "socket_connect_timeout": 5.0,
    "socket_keepalive": True,
    "health_check_interval": 30,
    "max_connections": 50,
}

# Errors that should trigger automatic retry
DEFAULT_RETRY_ERRORS: List[Type[Exception]] = [
    ConnectionError,
    TimeoutError,
    OSError,
]


def create_async_client(
    url: str,
    *,
    max_connections: int = DEFAULTS["max_connections"],
    socket_timeout: float = DEFAULTS["socket_timeout"],
    socket_connect_timeout: float = DEFAULTS["socket_connect_timeout"],
    health_check_interval: int = DEFAULTS["health_check_interval"],
    retry_attempts: int = 3,
    decode_responses: bool = True,
    **overrides,
):
    """
    Create a hardened async Redis client for FastAPI services.

    Args:
        url: Redis connection URL (redis://host:port/db)
        max_connections: Max pool connections (default 50)
        socket_timeout: Timeout for Redis operations in seconds (default 5.0)
        socket_connect_timeout: Timeout for initial connection in seconds (default 5.0)
        health_check_interval: Seconds between health checks (default 30)
        retry_attempts: Number of retry attempts with exponential backoff (default 3)
        decode_responses: Decode bytes to strings (default True)
        **overrides: Additional kwargs passed to redis.from_url()

    Returns:
        redis.asyncio.Redis instance with hardened connection settings
    """
    import redis.asyncio as aioredis
    from redis.asyncio.retry import Retry
    from redis.backoff import ExponentialBackoff

    retry = Retry(ExponentialBackoff(), retries=retry_attempts)

    params = {
        "encoding": DEFAULTS["encoding"],
        "decode_responses": decode_responses,
        "socket_timeout": socket_timeout,
        "socket_connect_timeout": socket_connect_timeout,
        "socket_keepalive": True,
        "health_check_interval": health_check_interval,
        "max_connections": max_connections,
        "retry": retry,
        "retry_on_error": DEFAULT_RETRY_ERRORS,
    }
    params.update(overrides)

    return aioredis.from_url(url, **params)


def create_sync_client(
    url: str,
    *,
    max_connections: int = DEFAULTS["max_connections"],
    socket_timeout: float = DEFAULTS["socket_timeout"],
    socket_connect_timeout: float = DEFAULTS["socket_connect_timeout"],
    health_check_interval: int = DEFAULTS["health_check_interval"],
    retry_attempts: int = 5,
    decode_responses: bool = True,
    **overrides,
):
    """
    Create a hardened sync Redis client for workers and background services.

    For BLPOP workers, set socket_timeout > blpop_timeout (e.g., 30.0 for 5s BLPOP).

    Args:
        url: Redis connection URL (redis://host:port/db)
        max_connections: Max pool connections (default 50)
        socket_timeout: Timeout for Redis operations in seconds (default 5.0).
                        For BLPOP workers, set this higher than the BLPOP timeout.
        socket_connect_timeout: Timeout for initial connection in seconds (default 5.0)
        health_check_interval: Seconds between health checks (default 30)
        retry_attempts: Number of retry attempts with exponential backoff (default 5)
        decode_responses: Decode bytes to strings (default True)
        **overrides: Additional kwargs passed to redis.Redis.from_url()

    Returns:
        redis.Redis instance with hardened connection settings
    """
    import redis as sync_redis
    from redis.retry import Retry
    from redis.backoff import ExponentialBackoff

    retry = Retry(ExponentialBackoff(base=0.1, cap=2), retries=retry_attempts)

    params = {
        "encoding": DEFAULTS["encoding"],
        "decode_responses": decode_responses,
        "socket_timeout": socket_timeout,
        "socket_connect_timeout": socket_connect_timeout,
        "socket_keepalive": True,
        "health_check_interval": health_check_interval,
        "max_connections": max_connections,
        "retry": retry,
        "retry_on_error": [ConnectionError, TimeoutError],
    }
    params.update(overrides)

    return sync_redis.Redis.from_url(url, **params)
