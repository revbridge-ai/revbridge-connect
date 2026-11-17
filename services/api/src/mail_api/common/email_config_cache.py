"""
Email configuration cache in Redis

Functions to read email configurations through the Redis cache, for fast access to
the SMTP settings.

Redis keys used:
- email_config:{config_id} - Configuration by ID
- email_config_domain:{domain_id} - Default configuration of a domain
- domain_id:{domain_name} - Domain name to domain_id mapping
"""

import json
import logging
from typing import Any

from mail_api.common.config import EMAIL_CONFIG_CACHE_TTL_SEC, REDIS_TTL_SEC
from mail_api.common.redis_client import redis_client, safe_setex

logger = logging.getLogger(__name__)


async def get_domain_config_from_redis(domain_id: int) -> dict[str, Any] | None:
    """
    Returns the default configuration of a domain from Redis.
    On a miss, reads it from the database and stores it in Redis with a TTL
    (EMAIL_CONFIG_CACHE_TTL_SEC): without a TTL, a direct UPDATE in the database
    would leave the cache stale forever (sending to an old smtp_host).

    Args:
        domain_id: Domain ID (virtual_domains.id)

    Returns:
        dict: Email configuration, or None if not found
    """
    try:
        if not redis_client:
            return None

        key = f"email_config_domain:{domain_id}"
        cached_data = await redis_client.get(key)

        if cached_data:
            return json.loads(cached_data)

        # Not in Redis: read it from the database and store it in Redis with a TTL
        logger.debug(f"Configuration not found in Redis for domain_id {domain_id}, reading it from the database")
        try:
            from mail_api.common.config import get_email_config_for_domain
            db_config = await get_email_config_for_domain(domain_id)

            if db_config:
                await safe_setex(
                    key,
                    db_config,
                    ttl=EMAIL_CONFIG_CACHE_TTL_SEC,
                    redis_instance=redis_client
                )
                logger.info(f"Configuration of domain_id {domain_id} read from the database and stored in Redis (ttl={EMAIL_CONFIG_CACHE_TTL_SEC}s)")
                return db_config
        except Exception as db_error:
            logger.warning(f"Error reading the configuration from the database for domain_id {domain_id}: {db_error}")

        return None
    except Exception as e:
        logger.debug(f"Error reading the domain configuration from Redis (domain_id={domain_id}): {e}")
        return None


async def get_domain_id_from_redis(domain_name: str) -> int | None:
    """
    Returns the domain_id for a domain name from Redis

    Args:
        domain_name: Domain name (e.g. example.com)

    Returns:
        int: Domain ID, or None if not found
    """
    try:
        if not redis_client:
            return None

        key = f"domain_id:{domain_name}"
        domain_id_str = await redis_client.get(key)

        if domain_id_str:
            return int(domain_id_str)

        return None
    except Exception as e:
        logger.debug(f"Error reading domain_id from Redis (domain={domain_name}): {e}")
        return None


async def get_email_config_for_domain_name_from_redis(domain_name: str) -> dict[str, Any] | None:
    """
    Returns the full email configuration of a domain, by name, from Redis.
    On a miss, reads it from the database and stores it in Redis with a TTL.

    Convenience function for the email_processor to look up a configuration by domain_name.

    Args:
        domain_name: Domain name (e.g. example.com)

    Returns:
        dict: Full email configuration, or None if not found
    """
    try:
        if not domain_name:
            return None

        # Try the cached domain_id first
        domain_id = await get_domain_id_from_redis(domain_name)

        # Not in Redis: read it from the database and cache it
        if not domain_id:
            try:
                from mail_api.common.db import fetch_one_async
                domain_row = await fetch_one_async(
                    "SELECT id FROM virtual_domains WHERE name = %s LIMIT 1",
                    (domain_name,)
                )
                if domain_row:
                    domain_id = domain_row['id']
                    # Cache the domain_id in Redis (with a TTL, it is only a mapping)
                    try:
                        await redis_client.setex(
                            f"domain_id:{domain_name}", REDIS_TTL_SEC, str(domain_id)
                        )
                    except:
                        pass
            except Exception as e:
                logger.debug(f"Error reading domain_id from the database for {domain_name}: {e}")
                return None

        if domain_id:
            # Domain configuration from Redis (falls back to the database on a miss)
            return await get_domain_config_from_redis(domain_id)

        return None
    except Exception as e:
        logger.debug(f"Error reading the full configuration from Redis (domain={domain_name}): {e}")
        return None


async def get_fallback_configs_for_domain(domain_name: str) -> list:
    """
    Returns the fallback configs (non-default, active) of a domain, ordered by priority DESC.
    Used when sending with the default config fails with a 4xx (deferred).

    Returns:
        list[dict]: Fallback configs, or an empty list
    """
    try:
        domain_id = await get_domain_id_from_redis(domain_name)

        if not domain_id:
            try:
                from mail_api.common.db import fetch_one_async
                domain_row = await fetch_one_async(
                    "SELECT id FROM virtual_domains WHERE name = %s LIMIT 1",
                    (domain_name,)
                )
                if domain_row:
                    domain_id = domain_row['id']
                else:
                    return []
            except Exception:
                return []

        from mail_api.common.db import fetch_all_async
        rows = await fetch_all_async(
            """
            SELECT smtp_host, smtp_port, smtp_secure, smtp_user, smtp_password,
                   smtp_base_port, smtp_timeout, envelope_prefix, config_id, name, priority
            FROM email_configs
            WHERE domain_id = %s AND is_default = FALSE AND is_active = TRUE
            ORDER BY priority DESC
            """,
            (domain_id,)
        )
        return [dict(row) for row in rows] if rows else []
    except Exception as e:
        logger.warning(f"Error fetching the fallback configs for {domain_name}: {e}")
        return []
