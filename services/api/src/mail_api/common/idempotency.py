"""Email send idempotency.

The caller sends an `Idempotency-Key` header (for example `campaign:user:decision`),
and retries (HTTP clients, job runners, gateways) get the cached response instead of
enqueuing the email again.

Race-safe through SETNX (claim_idempotency_key). Fail-open: a Redis error does NOT
block the send; it only loses the dedupe protection for that request.
"""
from __future__ import annotations

import json
import logging

from mail_api.common.redis_client import redis_client

logger = logging.getLogger(__name__)

IDEMPOTENCY_PREFIX = "mail:idem:"
IDEMPOTENCY_TTL = 3600  # 1h covers exponential backoff and typical retry windows


async def get_idempotent_response(idempotency_key: str) -> dict | None:
    """Cached response for the idempotency key, or None if there is none."""
    if not idempotency_key or redis_client is None:
        return None
    try:
        data = await redis_client.get(f"{IDEMPOTENCY_PREFIX}{idempotency_key}")
        if data:
            return json.loads(data)
        return None
    except Exception as e:
        logger.warning("[IDEMPOTENCY] lookup failed key=%s: %s", idempotency_key, e)
        return None


async def claim_idempotency_key(idempotency_key: str, response: dict) -> bool:
    """SETNX of the response under the idempotency key.

    Returns True if we reserved it (the caller should go on and enqueue), False if
    another process already reserved it (the caller should GET and return its response).
    Fail-open: a Redis error returns True so sends are not blocked.
    """
    if not idempotency_key or redis_client is None:
        return True
    try:
        ok = await redis_client.set_nx(
            f"{IDEMPOTENCY_PREFIX}{idempotency_key}",
            json.dumps(response, default=str),
            IDEMPOTENCY_TTL,
        )
        return bool(ok)
    except Exception as e:
        logger.warning("[IDEMPOTENCY] claim failed key=%s: %s (fail-open)", idempotency_key, e)
        return True


async def release_idempotency_key(idempotency_key: str) -> None:
    """DELETE of the key after an enqueue failure.

    Without it, the claim made before the enqueue is orphaned: the caller's retry
    (same Idempotency-Key) gets the cached "queued" answer for an email that never
    reached the queue, a silent loss. Best-effort: a Redis error is only logged
    (if Redis is down, the claim most likely did not persist either).
    """
    if not idempotency_key or redis_client is None:
        return
    try:
        await redis_client.delete(f"{IDEMPOTENCY_PREFIX}{idempotency_key}")
    except Exception as e:
        logger.warning("[IDEMPOTENCY] release failed key=%s: %s", idempotency_key, e)


async def store_idempotent_response(idempotency_key: str, response: dict) -> None:
    """Overwrites the cache with the authoritative response (after the enqueue),
    keeping the TTL. Best-effort: a Redis error is only logged."""
    if not idempotency_key or redis_client is None:
        return
    try:
        await redis_client.setex(
            f"{IDEMPOTENCY_PREFIX}{idempotency_key}",
            IDEMPOTENCY_TTL,
            json.dumps(response, default=str),
        )
    except Exception as e:
        logger.warning("[IDEMPOTENCY] store failed key=%s: %s", idempotency_key, e)
