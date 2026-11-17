"""Redis connection helper for the deliverability jobs."""

from __future__ import annotations

import logging

import redis

logger = logging.getLogger(__name__)


def create_redis_client(
    redis_url: str,
    *,
    decode_responses: bool = True,
    socket_connect_timeout: float = 5.0,
    socket_timeout: float = 5.0,
    health_check_interval: int = 30,
    for_blocking_operations: bool = False,
) -> redis.Redis:
    """Create a Redis client using the shared connect-lib factory.

    Args:
        redis_url: Redis connection URL
        decode_responses: Whether to decode responses as strings
        socket_connect_timeout: Timeout for socket connection in seconds
        socket_timeout: Timeout for socket operations in seconds
        health_check_interval: Interval for health checks in seconds
        for_blocking_operations: If True, increases socket_timeout to 15s for blocking ops (blpop/brpop)

    Returns:
        Configured Redis client instance with hardened settings (keepalive, retry, etc.)
    """
    # For blocking operations, ensure socket_timeout is sufficient
    if for_blocking_operations and socket_timeout <= 5:
        socket_timeout = 15.0

    from connect_lib.redis_client import create_sync_client

    return create_sync_client(
        redis_url,
        decode_responses=decode_responses,
        socket_connect_timeout=socket_connect_timeout,
        socket_timeout=socket_timeout,
        health_check_interval=health_check_interval,
    )
