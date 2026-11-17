"""Event-payload field mapping for the webhook dispatcher.

Splits an incoming engagement payload into two buckets:

  * `specific_fields` — flat columns the webhook persistence layer stores
    in dedicated DB columns (e.g. `bounce_reason`, `delivery_time`).
  * `general_data`    — everything else, packed into the `data` blob.

The split is event-type-aware: each event type has its own list of
"promoted" fields. The `delivered` event additionally normalizes
`delivery_time` to a `datetime` (ISO-8601 strings + `Z` suffix accepted),
falling back to `None` on parse failure.

Pure module — no `self`, no I/O — so it's exhaustively unit-testable.
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

logger = logging.getLogger(__name__)

# Per-event "promoted" field lists. Anything in this list moves from the
# raw payload into the typed `specific_fields` dict; everything else
# falls through to `general_data`.
EVENT_FIELD_MAP: dict[str, list[str]] = {
    "delivered": ["delivery_time", "server_response"],
    "sent": ["subject", "sender_email", "recipient_count"],
    "bounced": ["bounce_type", "bounce_reason", "bounce_code", "server_response"],
    "soft_bounced": ["bounce_type", "bounce_reason", "bounce_code", "server_response"],
    "hard_bounced": ["bounce_type", "bounce_reason", "bounce_code", "server_response"],
    "opened": ["user_agent", "ip_address", "location"],
    "clicked": ["clicked_url", "user_agent", "ip_address", "location"],
    "complaint": ["complaint_type", "complaint_feedback", "source_ip"],
    "unsubscribed": ["unsubscribe_reason", "unsubscribe_url"],
}


def _split_payload(
    payload_data: dict[str, Any],
    promoted_fields: list[str],
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Bucket payload keys into (promoted, leftover)."""
    promoted_set = set(promoted_fields)
    specific = {field: payload_data.get(field) for field in promoted_fields}
    general = {k: v for k, v in payload_data.items() if k not in promoted_set}
    return specific, general


def _normalize_delivery_time(
    raw: Any,
    *,
    log_context: str = "",
) -> datetime | None:
    """Coerce a delivery_time value to `datetime | None`.

    Accepts ISO-8601 strings (`Z` suffix swapped for `+00:00`) and naive
    datetimes. Anything else logs a warning and returns None — better to
    drop a malformed timestamp than to crash the whole event pipeline.
    """
    if not raw:
        return None
    if isinstance(raw, datetime):
        return raw
    if isinstance(raw, str):
        try:
            return datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except (ValueError, AttributeError) as e:
            logger.warning(
                "%sError converting delivery_time '%s' to datetime: %s",
                log_context, raw, e,
            )
            return None
    logger.warning("%sdelivery_time in an unexpected format: %s", log_context, type(raw))
    return None


def split_event_fields(
    event_type: str,
    payload_data: dict[str, Any],
    *,
    worker_name: str | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Split an event payload into (specific_fields, general_data).

    Args:
        event_type: lowercased event type (e.g. "delivered", "bounced").
        payload_data: raw payload received from Redis/Kafka.
        worker_name: optional logger context for warnings (kept for
            parity with the original `[worker-N] ...` log format).

    Returns:
        (specific_fields, general_data). For unknown event types, the
        specific_fields dict is empty and the entire payload falls into
        general_data.
    """
    promoted = EVENT_FIELD_MAP.get(event_type)
    if promoted is None:
        return {}, payload_data

    specific, general = _split_payload(payload_data, promoted)

    # The only event that needs post-processing today is `delivered`,
    # which normalizes delivery_time. Other events are already typed
    # correctly upstream.
    if event_type == "delivered":
        log_context = f"[{worker_name}] " if worker_name else ""
        specific["delivery_time"] = _normalize_delivery_time(
            specific.get("delivery_time"),
            log_context=log_context,
        )

    return specific, general


__all__ = ["split_event_fields", "EVENT_FIELD_MAP"]
