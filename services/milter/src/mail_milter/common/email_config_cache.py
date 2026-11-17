"""
Email configuration cache in Redis, for the milter.

Synchronous helpers that read email configurations from the Redis cache, so the
milter gets SMTP/tracking settings without a database round-trip.

Redis keys used (shared with the tracking service):
- email_config:{config_id} - one configuration, by ID
- email_config_domain:{domain_id} - the default configuration of a domain

Note: the tracking service does NOT store domain_id:{domain_name} in Redis.
When needed, domain_id is read from the database instead.
"""

import json
import logging
from typing import Any

from mail_milter.common.config import EMAIL_CONFIG_CACHE_TTL_SEC
from mail_milter.common.redis_client import redis

logger = logging.getLogger(__name__)


def get_domain_config_from_redis(domain_id: int) -> dict[str, Any] | None:
    """
    Get a domain's default configuration from Redis (synchronous).
    If it is not in Redis, read it from the database and store it in Redis.

    Args:
        domain_id: Domain ID (virtual_domains.id)

    Returns:
        dict: Email configuration, or None if not found
    """
    try:
        if not redis:
            logger.debug(
                f"[REDIS] Redis unavailable to fetch the configuration of domain_id {domain_id}"
            )
            return None

        key = f"email_config_domain:{domain_id}"
        cached_data = redis.get(key)

        if cached_data:
            config = json.loads(cached_data)
            logger.debug(
                f"[REDIS] Configuration found in Redis for domain_id {domain_id} (key: {key})"
            )
            return config

        logger.debug(
            f"[REDIS] Configuration NOT found in Redis for domain_id {domain_id} (key: {key}), reading from the database"
        )

        # Fallback: read from the database and cache it in Redis
        try:
            from mail_milter.common.db import fetch_one

            sql = """
                SELECT id, config_id, domain_id, name,
                    smtp_host, smtp_port, smtp_secure, smtp_user, smtp_password, smtp_base_port, smtp_timeout,
                    tracking_enabled, inject_pixel, rewrite_links, pixel_tracking_url, click_tracking_url, tracking_domain,
                    default_from_name, default_from_email, default_reply_to,
                    bounce_email, complaint_email, shadow_email,
                    rate_limit_per_minute, rate_limit_per_hour, rate_limit_per_day,
                    max_retries, retry_delay_seconds,
                    default_webhook_url, default_webhook_secret,
                    additional_config, is_active, is_default, priority, notes,
                    created_by, created_at, updated_at
                FROM revbridge_connect.email_configs
                WHERE domain_id = %s AND is_default = TRUE AND is_active = TRUE
                ORDER BY priority DESC, created_at DESC
                LIMIT 1
            """
            result = fetch_one(sql, (domain_id,))
            if result:
                config = dict(result)
                # Serialize datetimes to JSON
                for k, v in config.items():
                    if hasattr(v, 'isoformat'):
                        config[k] = v.isoformat()
                # Cache in Redis with a TTL: without one, a direct UPDATE in the
                # database would leave the cache stale forever
                try:
                    redis.setex(key, EMAIL_CONFIG_CACHE_TTL_SEC, json.dumps(config))
                    logger.info(
                        f"[DB->REDIS] Configuration of domain_id {domain_id} read from the database and stored in Redis"
                    )
                except Exception as cache_err:
                    logger.debug(
                        f"[REDIS] Error caching the configuration of domain_id {domain_id}: {cache_err}"
                    )
                return config
        except Exception as db_error:
            logger.warning(
                f"[DB] Error reading the configuration from the database for domain_id {domain_id}: {db_error}"
            )

        return None
    except Exception as e:
        logger.debug(
            f"Error reading the domain configuration from Redis (domain_id={domain_id}): {e}")
        return None


def get_domain_id_from_redis(domain_name: str) -> int | None:
    """
    Get domain_id from Redis by domain name (synchronous)

    Args:
        domain_name: Domain name (e.g. example.com)

    Returns:
        int: Domain ID, or None if not found
    """
    try:
        if not redis:
            logger.debug(
                f"[REDIS] Redis unavailable to fetch the domain_id of {domain_name}"
            )
            return None

        key = f"domain_id:{domain_name}"
        domain_id_str = redis.get(key)

        if domain_id_str:
            domain_id = int(domain_id_str)
            logger.debug(
                f"[REDIS] domain_id found in Redis: {key} -> {domain_id}"
            )
            return domain_id

        logger.debug(
            f"[REDIS] domain_id NOT found in Redis for key: {key}"
        )
        return None
    except Exception as e:
        logger.debug(
            f"Error reading domain_id from Redis (domain={domain_name}): {e}")
        return None


def get_domain_id(domain_name: str) -> int | None:
    """
    Get domain_id by domain name, trying Redis first and then the database

    Args:
        domain_name: Domain name (e.g. example.com)

    Returns:
        int: Domain ID, or None if not found
    """
    if not domain_name or domain_name == "unknown":
        return None

    # Normalize domain_name
    domain_name = domain_name.lower().strip()

    # Try Redis first
    domain_id = get_domain_id_from_redis(domain_name)

    # Not in Redis: try the database
    if not domain_id:
        try:
            from mail_milter.common.db import fetch_one

            sql = """
                SELECT id
                FROM revbridge_connect.virtual_domains
                WHERE name = %s
            """
            result = fetch_one(sql, (domain_name,))
            if result:
                domain_id = result.get('id')
                logger.debug(
                    f"[DB] domain_id found in the database for {domain_name}: {domain_id}"
                )
        except Exception as db_error:
            logger.debug(
                f"[DB] Error reading domain_id from the database for {domain_name}: {db_error}"
            )

    return domain_id


def get_email_config_for_domain_from_redis(domain_name: str) -> dict[str, Any] | None:
    """
    Get the full email configuration of a domain from Redis (synchronous)

    Since the tracking service does not store domain_id:{domain_name} in Redis,
    this function:
    1. Looks up domain_id in Redis (may not exist)
    2. If not found, reads domain_id from the database
    3. Reads email_config_domain:{domain_id} from Redis

    Args:
        domain_name: Domain name (e.g. example.com)

    Returns:
        dict: Full email configuration, or None if not found
    """
    try:
        if not redis:
            logger.debug(
                f"[REDIS] Redis unavailable to fetch the configuration of domain {domain_name}"
            )
            return None

        # Normalize domain_name
        domain_name = domain_name.lower().strip()

        # Try Redis first for domain_id
        domain_id = get_domain_id_from_redis(domain_name)

        # Not in Redis: try the database
        if not domain_id:
            try:
                from mail_milter.common.db import fetch_one

                sql = """
                    SELECT id
                    FROM revbridge_connect.virtual_domains
                    WHERE name = %s
                """
                result = fetch_one(sql, (domain_name,))
                if result:
                    domain_id = result.get('id')
                    logger.debug(
                        f"[DB] domain_id found in the database for {domain_name}: {domain_id}"
                    )
            except Exception as db_error:
                logger.debug(
                    f"[DB] Error reading domain_id from the database for {domain_name}: {db_error}"
                )

        if not domain_id:
            logger.debug(
                f"[REDIS] domain_id not found (Redis or DB) for domain {domain_name}"
            )
            return None

        logger.debug(
            f"[REDIS] Using domain_id {domain_id} to fetch the configuration"
        )

        # Read the domain's full configuration from Redis
        config = get_domain_config_from_redis(domain_id)
        if not config:
            logger.debug(
                f"[REDIS] Configuration not found in Redis for domain_id {domain_id} (key: email_config_domain:{domain_id})"
            )
        return config
    except Exception as e:
        logger.debug(
            f"Error reading the full configuration from Redis (domain={domain_name}): {e}")
        return None


def get_approved_sender_from_redis(sender_email: str, principal_domain_id: int) -> dict[str, Any] | None:
    """
    Get approved-sender information from Redis (synchronous)

    Args:
        sender_email: Full sender address (e.g. news@mail.example.com)
        principal_domain_id: ID of the principal domain

    Returns:
        dict: Approved-sender information with principal_domain_id, or None if not found
    """
    try:
        if not redis:
            logger.debug(
                f"[REDIS] Redis unavailable to fetch approved sender {sender_email}"
            )
            return None

        # Normalize sender_email
        sender_email = sender_email.lower().strip()

        key = f"approved_sender:{sender_email}:{principal_domain_id}"
        cached_data = redis.get(key)

        if cached_data:
            sender_info = json.loads(cached_data)
            logger.debug(
                f"[REDIS] Approved sender found in Redis: {key} -> principal_domain_id={sender_info.get('principal_domain_id')}"
            )
            return sender_info

        logger.debug(
            f"[REDIS] Approved sender NOT found in Redis for key: {key}"
        )
        return None
    except Exception as e:
        logger.debug(
            f"Error reading approved sender from Redis (sender_email={sender_email}, domain_id={principal_domain_id}): {e}")
        return None
