"""
API event outbox (kafka_outbox table).

Builds normalized events with connect_lib.messaging and writes one row per event to
kafka_outbox; the webhook dispatcher reads them. Despite the table name, no Kafka broker
is involved. The public interface is kept for compatibility with existing code.

Email events: opened, clicked, unsubscribed, failed (the queue processor writes
`failed` for permanent failures that happen before Postfix).
"""

import json
import logging
import os
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

# connect_lib.messaging imports
from connect_lib.messaging import (
    CampaignInfo,
    ChannelInfo,
    ChannelType,
    ContactInfo,
    DeviceInfo,
    EngagementAction,
    EngagementMetadata,
    Environment,
    GeoInfo,
    IdentifierType,
    OutboxMessage,
    ProviderType,
    TenantInfo,
    UnifiedEngagementEvent,
    create_event_id,
)

from .db import get_conn


class ServiceType(StrEnum):
    """Service type of an outbox event (Connect: email)"""
    EMAIL = "email"


class KafkaEventType(StrEnum):
    """Email event types that can be written to the outbox (compatibility)"""

    OPENED = "opened"
    CLICKED = "clicked"
    UNSUBSCRIBED = "unsubscribed"
    # Permanent failure BEFORE Postfix (worker): the milter never sees the message,
    # so the event comes from the queue processor itself.
    FAILED = "failed"


# KafkaEventType to EngagementAction mapping (Email)
EMAIL_EVENT_TYPE_TO_ACTION: dict[str, EngagementAction] = {
    "opened": EngagementAction.OPENED,
    "clicked": EngagementAction.CLICK,
    "unsubscribed": EngagementAction.UNSUBSCRIBED,
    "failed": EngagementAction.FAILED,
}


def _create_email_unified_event(
    event_type: KafkaEventType,
    message_id: str,
    recipient_email: str,
    domain_name: str,
    payload_data: dict[str, Any],
) -> UnifiedEngagementEvent:
    """
    Builds a normalized UnifiedEngagementEvent for email tracking.

    Args:
        event_type: Event type (opened, clicked, unsubscribed, failed)
        message_id: Message ID
        recipient_email: Recipient email
        domain_name: Sending domain
        payload_data: Additional event data

    Returns:
        Normalized UnifiedEngagementEvent
    """
    # Map event_type to EngagementAction
    action = EMAIL_EVENT_TYPE_TO_ACTION.get(event_type.value, EngagementAction.OPENED)

    # Geo/device fields from the payload
    geo_info = None
    if payload_data.get("location") or payload_data.get("ip_address"):
        geo_info = GeoInfo(
            ip=payload_data.get("ip_address"),
        )

    device_info = None
    if payload_data.get("user_agent"):
        device_info = DeviceInfo(
            user_agent=payload_data.get("user_agent"),
        )

    # Click URL for click events
    click_url = payload_data.get("clicked_url") if event_type == KafkaEventType.CLICKED else None

    # campaign_id: passed through from the X-Connect-Campaign header the client
    # itself sends with the email (same as the milter's kafka_client).
    campaign_id = payload_data.get("campaign_id")
    campaign_info = (
        CampaignInfo(id=campaign_id, message_id=message_id)
        if campaign_id else None
    )

    # Build the normalized event
    return UnifiedEngagementEvent(
        event_id=create_event_id(),
        event_type="engagement",
        timestamp=datetime.now(UTC),
        channel=ChannelInfo(
            type=ChannelType.EMAIL,
            provider=ProviderType.POSTFIX,
            provider_event_id=message_id,
        ),
        action=action,
        contact=ContactInfo(
            identifier=recipient_email or "",
            identifier_type=IdentifierType.EMAIL,
        ),
        campaign=campaign_info,
        metadata=EngagementMetadata(
            click_url=click_url,
            domain_name=domain_name,
            geo=geo_info,
            device=device_info,
            error_code=payload_data.get("error_code"),
            error_message=payload_data.get("error_message"),
            provider_data={
                "domain_name": domain_name,
                **{k: v for k, v in payload_data.items() if k not in ("user_agent", "ip_address", "clicked_url", "error_code", "error_message")},
            },
        ),
        tenant=TenantInfo(
            id=domain_name or "default",
            environment=Environment.PRODUCTION,
        ),
        raw_payload=payload_data,
    )


class KafkaClient:
    """Writes normalized events to the event outbox"""

    def __init__(self):
        self.logger = logging.getLogger(__name__)
        # RevBridge Connect: CONNECT_EVENT_OUTBOX turns on writing events to the outbox; the
        # webhook dispatcher (mail_api.webhook_dispatch) reads them
        self.enabled = os.getenv("CONNECT_EVENT_OUTBOX", "false").strip().lower() in ("1", "true", "yes", "on")

    def _insert_into_outbox(
        self,
        event: UnifiedEngagementEvent,
        event_type: str,
        message_id: str,
        rcpt: str | None,
        domain_name: str | None,
        service_type: ServiceType = ServiceType.EMAIL,
        client_id: str | None = None,
    ) -> int | None:
        """
        Inserts a normalized event into the outbox table.

        Args:
            event: Normalized event
            event_type: Event type (string)
            message_id: Message ID
            rcpt: Recipient email
            domain_name: Sending domain
            service_type: Service type (email)
            client_id: Customer client_id

        Returns:
            int or None: ID of the inserted row, or None on error
        """
        try:
            if not self.enabled:
                self.logger.debug(
                    f"Event outbox disabled - event {event_type} will not be added for {message_id}"
                )
                return None

            # Convert the event to the outbox format
            outbox_message = OutboxMessage.from_event(event, include_raw_payload=True)

            # Serialize the payload
            payload_json = json.dumps(outbox_message.payload, default=str)

            # Insert into the outbox
            sql = """
                INSERT INTO kafka_outbox
                (service_type, event_type, message_id, rcpt, domain_name, client_id, payload, status)
                VALUES (%s, %s, %s, %s, %s, %s, %s, 'pending')
            """
            params = (
                service_type.value,
                event_type if isinstance(event_type, str) else event_type.value,
                message_id,
                rcpt,
                domain_name,
                client_id,
                payload_json,
            )

            with get_conn() as conn:
                with conn.cursor() as cur:
                    cur.execute(sql, params)
                    outbox_id = cur.lastrowid
                conn.commit()

            self.logger.debug(
                f"Normalized event {service_type.value}/{event_type} added to the outbox (ID: {outbox_id}) for {message_id}"
            )
            return outbox_id

        except Exception as e:
            self.logger.error(
                f"Error adding an event to the outbox - Service: {service_type.value}, Event: {event_type}, Message ID: {message_id}, Error: {e}",
                exc_info=True,
            )
            return None

    def queue_kafka_event(
        self,
        event_type: KafkaEventType,
        message_id: str,
        recipient_email: str | None,
        domain_name: str,
        payload_data: dict[str, Any],
    ) -> bool:
        """
        Adds a normalized email event to the outbox.

        Args:
            event_type: Event type (opened, clicked, unsubscribed, failed)
            message_id: Message ID
            recipient_email: Recipient email
            domain_name: Domain name
            payload_data: Payload data

        Returns:
            bool: True if the event was added
        """
        try:
            # Build the normalized event with connect_lib.messaging
            event = _create_email_unified_event(
                event_type=event_type,
                message_id=message_id,
                recipient_email=recipient_email or "",
                domain_name=domain_name,
                payload_data=payload_data,
            )

            outbox_id = self._insert_into_outbox(
                event=event,
                event_type=event_type.value,
                message_id=message_id,
                rcpt=recipient_email,
                domain_name=domain_name,
            )

            if outbox_id:
                self.logger.info(
                    f"Normalized email event: type={event_type.value} message_id={message_id} outbox_id={outbox_id} event_id={event.event_id}"
                )

            return outbox_id is not None

        except Exception as e:
            self.logger.error(
                f"Error adding an event to the outbox - Event: {event_type}, Message ID: {message_id}, Error: {e}",
                exc_info=True,
            )
            return False

    def queue_email_failed(
        self,
        message_id: str,
        recipient_email: str,
        domain_name: str,
        error_message: str = None,
        error_code: str = None,
        campaign_id: str = None,
        **extra_data,
    ) -> bool:
        """PERMANENT failure before Postfix (email_processor).

        Delivery is normally audited by the milter, but a message that fails
        before Postfix never goes through it; without this event the failure is
        invisible to webhooks (action=failed).
        """
        payload_data = {
            "error_message": error_message,
            "error_code": error_code,
            "campaign_id": campaign_id,
            **extra_data,
        }

        return self.queue_kafka_event(
            event_type=KafkaEventType.FAILED,
            message_id=message_id,
            recipient_email=recipient_email,
            domain_name=domain_name,
            payload_data=payload_data,
        )


# Global client instance
kafka_client = KafkaClient()
