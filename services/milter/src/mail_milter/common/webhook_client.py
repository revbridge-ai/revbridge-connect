"""
Webhook client of the milter service.

Writes webhook events straight into the Redis queue (`w:*` keys) that the
webhook dispatcher reads.
"""

import json
import logging
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from .config import REDIS_TTL_SEC
from .redis_client import redis


class WebhookEventType(StrEnum):
    """Email event types that can trigger webhooks"""

    SENT = "sent"
    DELIVERED = "delivered"
    BOUNCED = "bounced"
    SOFT_BOUNCED = "soft_bounced"
    HARD_BOUNCED = "hard_bounced"
    DEFERRED = "deferred"
    SUPPRESSED = "suppressed"
    COMPLAINT = "complaint"  # spam complaint (ARF/FBL)


class WebhookClient:
    """Client that writes webhook events to the Redis queue"""

    def __init__(self):
        self.logger = logging.getLogger(__name__)

    def queue_webhook_event(
        self,
        event_type: WebhookEventType,
        message_id: str,
        payload_data: dict[str, Any],
        domain_name: str,
        recipient_email: str = None,
    ) -> bool:
        """
        Add a generic event to the Redis queue, for the webhook dispatcher

        The dispatcher looks up every active webhook for this event type
        and calls each of them.

        Args:
            event_type: Event type (sent, delivered, bounced, etc.)
            message_id: Message ID
            payload_data: Payload data (headers are moved to message_headers)
            recipient_email: Recipient address
            domain_name: Domain name

        Returns:
            bool: True if the event was queued
        """
        try:
            if redis is None:
                self.logger.error(
                    f"Redis unavailable - webhook {event_type} not queued for {message_id}"
                )
                return False

            self.logger.debug(f"Queuing webhook {event_type} for {message_id}")

            # Take the headers out of payload_data, if present
            message_headers = payload_data.pop("email_headers", {})

            # Generic event (not tied to a specific webhook ID)
            generic_event = {
                "event_type": event_type.value,
                "message_id": message_id,
                "timestamp": datetime.now(UTC).isoformat(),
                "recipient_email": recipient_email,
                "domain_name": domain_name,
                "payload_data": payload_data,
                "message_headers": message_headers,
                "source": "revbridge-connect",
                "created_at": datetime.now(UTC).isoformat(),
            }

            # Queue the generic event in Redis; the dispatcher processes it
            # and looks up the active webhooks
            event_json = json.dumps(generic_event, default=str)

            # Key prefixed with domain_name; recipient_email is part of the key
            # so that several recipients of one message do not collide
            queue_key = (
                f"w:{domain_name}:{event_type.value}:{message_id}:{recipient_email}"
            )
            result = redis.setex(queue_key, REDIS_TTL_SEC, event_json)

            if result:
                self.logger.debug(
                    f"Webhook {event_type} queued at '{queue_key}' for {message_id}"
                )
                return True
            else:
                self.logger.warning(
                    f"Webhook {event_type} not queued at '{queue_key}' (setex returned a falsy value) for {message_id}"
                )
                return False

        except Exception as e:
            self.logger.error(
                f"Error queuing webhook - Event: {event_type}, Message ID: {message_id}, Error: {e}",
                exc_info=True,
            )
            return False


# Global client instance
webhook_client = WebhookClient()
