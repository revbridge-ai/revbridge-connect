"""
Approved Sender cache in Redis

Functions to read approved senders through the Redis cache, for fast access to the
approved sender information.

Redis keys used:
- approved_sender:{sender_email}:{principal_domain_id} - Approved sender
  information by sender_email and principal_domain_id
"""

import json
import logging
from typing import Any

from mail_api.common.config import REDIS_TTL_SEC
from mail_api.common.redis_client import redis_client

logger = logging.getLogger(__name__)


async def get_approved_sender_from_redis(
    sender_email: str, principal_domain_id: int
) -> dict[str, Any] | None:
    """
    Returns the approved sender information from Redis by sender_email and principal_domain_id

    Args:
        sender_email: Full sender email (e.g. newsletter@example.com)
        principal_domain_id: ID of the authorized main domain

    Returns:
        dict: Approved sender information, or None if not found
            Fields returned:
            - id: Approved sender ID
            - sender_email: Full sender email
            - principal_domain_id: ID of the authorized main domain
            - is_active: Whether the sender is active
    """
    try:
        if not redis_client:
            return None

        # Normalize sender_email (lowercase, no spaces)
        normalized_email = sender_email.lower().strip()

        key = f"approved_sender:{normalized_email}:{principal_domain_id}"
        cached_data = await redis_client.get(key)

        if cached_data:
            sender_info = json.loads(cached_data)
            logger.debug(
                f"Approved sender found in Redis: "
                f"{key} -> principal_domain_id={sender_info.get('principal_domain_id')}"
            )
            return sender_info

        logger.debug(
            f"Approved sender NOT found in Redis for key: {key}"
        )
        return None
    except Exception as e:
        logger.debug(
            f"Error reading the approved sender from Redis "
            f"(sender_email={sender_email}, "
            f"principal_domain_id={principal_domain_id}): {e}"
        )
        return None


async def get_approved_sender_info(
    sender_email: str, principal_domain_id: int
) -> dict[str, Any] | None:
    """
    Returns the approved sender information (Redis first, then the database)

    Tries Redis first (fast); on a miss, reads the database and caches the
    result for next time.

    Args:
        sender_email: Full sender email (e.g. newsletter@example.com)
        principal_domain_id: ID of the authorized main domain

    Returns:
        dict: Approved sender information, or None if not found
    """
    try:
        # Try Redis first
        sender_info = await get_approved_sender_from_redis(
            sender_email, principal_domain_id
        )
        if sender_info:
            return sender_info

        # Fall back to the database
        from mail_api.common.db import fetch_one_async

        normalized_email = sender_email.lower().strip()
        sql = """
            SELECT id, sender_email, principal_domain_id, is_active
            FROM approved_senders
            WHERE sender_email = %s
            AND principal_domain_id = %s
            AND is_active = TRUE
        """
        result = await fetch_one_async(
            sql, (normalized_email, principal_domain_id)
        )

        if result:
            sender_info = {
                "id": result.get("id"),
                "sender_email": result.get("sender_email"),
                "principal_domain_id": result.get("principal_domain_id"),
                "is_active": result.get("is_active", True),
            }

            # Cache it in Redis for next time
            if redis_client:
                try:
                    key = (
                        f"approved_sender:{normalized_email}:"
                        f"{principal_domain_id}"
                    )
                    await redis_client.setex(
                        key, REDIS_TTL_SEC, json.dumps(sender_info)
                    )
                except Exception:
                    pass  # A cache failure must not block

            return sender_info

        return None
    except Exception as e:
        logger.debug(
            f"Error fetching the approved sender "
            f"(sender_email={sender_email}, "
            f"principal_domain_id={principal_domain_id}): {e}"
        )
        return None
