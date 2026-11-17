"""
Event outbox of the tracking service (table kafka_outbox; the name is historical).

Builds normalized events with connect_lib.messaging and writes one row per event to
kafka_outbox, which the webhook dispatcher reads. It does not talk to any Kafka broker.
The public interface is kept for compatibility with existing code.

Supports the email tracking events: opened, clicked, unsubscribed.
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
    """Email event types written to the outbox (name kept for compatibility)"""

    OPENED = "opened"
    CLICKED = "clicked"
    UNSUBSCRIBED = "unsubscribed"


# KafkaEventType -> EngagementAction mapping (email)
EMAIL_EVENT_TYPE_TO_ACTION: dict[str, EngagementAction] = {
    "opened": EngagementAction.OPENED,
    "clicked": EngagementAction.CLICK,
    "unsubscribed": EngagementAction.UNSUBSCRIBED,
}


def _create_email_unified_event(
    event_type: KafkaEventType,
    message_id: str,
    recipient_email: str,
    domain_name: str,
    payload_data: dict[str, Any],
    campaign_id: str | None = None,
) -> UnifiedEngagementEvent:
    """
    Build a normalized UnifiedEngagementEvent for email tracking.

    Args:
        event_type: Event type (opened, clicked, unsubscribed)
        message_id: Message ID
        recipient_email: Recipient address
        domain_name: Sending domain
        payload_data: Additional event data
        campaign_id: Campaign ID (the sender's X-Connect-Campaign header, optional)

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

    # Click URL, for click events
    click_url = payload_data.get("clicked_url") if event_type == KafkaEventType.CLICKED else None

    # CampaignInfo, when there is a campaign_id
    campaign_info = None
    if campaign_id:
        campaign_info = CampaignInfo(
            id=campaign_id,
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
            provider_data={
                "domain_name": domain_name,
                **{k: v for k, v in payload_data.items() if k not in ("user_agent", "ip_address", "clicked_url")},
            },
        ),
        tenant=TenantInfo(
            id=domain_name or "default",
            environment=Environment.PRODUCTION,
        ),
        raw_payload=payload_data,
    )


class KafkaClient:
    """Client that writes normalized events to the outbox table"""

    def __init__(self):
        self.logger = logging.getLogger(__name__)
        # CONNECT_EVENT_OUTBOX turns on writing events to the outbox; the webhook
        # dispatcher (mail_api.webhook_dispatch) reads them
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
        Insert a normalized event into the outbox table.

        Args:
            event: Normalized event
            event_type: Event type (string)
            message_id: Message ID
            rcpt: Recipient address
            domain_name: Domain name
            service_type: Service type (email by default)
            client_id: Client ID (optional column)

        Returns:
            int or None: ID of the inserted row, or None on error
        """
        try:
            if not self.enabled:
                self.logger.debug(
                    f"Event outbox disabled - event {event_type} not written for {message_id}"
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
                f"Normalized event {service_type.value}/{event_type} written to the outbox (ID: {outbox_id}) for {message_id}"
            )
            return outbox_id

        except Exception as e:
            self.logger.error(
                f"Error writing event to the outbox - Service: {service_type.value}, Event: {event_type}, Message ID: {message_id}, Error: {e}",
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
        campaign_id: str | None = None,
    ) -> bool:
        """
        Write a normalized email tracking event to the outbox.

        Args:
            event_type: Event type (opened, clicked, unsubscribed)
            message_id: Message ID
            recipient_email: Recipient address
            domain_name: Domain name
            payload_data: Payload data
            campaign_id: Campaign ID (the sender's X-Connect-Campaign header, optional)

        Returns:
            bool: True if the event was written
        """
        try:
            # Build the normalized event with connect_lib.messaging
            event = _create_email_unified_event(
                event_type=event_type,
                message_id=message_id,
                recipient_email=recipient_email or "",
                domain_name=domain_name,
                payload_data=payload_data,
                campaign_id=campaign_id,
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
                    f"log_type=audit action=tracking_event_outbox_enqueued "
                    f"event_type={event_type.value} message_id={message_id} "
                    f"recipient={recipient_email} domain_name={domain_name} "
                    f"outbox_id={outbox_id} event_id={event.event_id}"
                )

            return outbox_id is not None

        except Exception as e:
            self.logger.error(
                f"Error writing event to the outbox - Event: {event_type}, Message ID: {message_id}, Error: {e}",
                exc_info=True,
            )
            return False

    def queue_email_opened(
        self,
        message_id: str,
        recipient_email: str,
        domain_name: str,
        user_agent: str = None,
        ip_address: str = None,
        referer: str = None,
        location: str = None,
        open_source: str = None,
        open_status: str = None,
        open_confidence_score: int = None,
        campaign_id: str = None,
        **extra_data,
    ) -> bool:
        """Write an email-opened event to the outbox."""
        payload_data = {
            "user_agent": user_agent,
            "ip_address": ip_address,
            "referer": referer,
            "location": location,
            "open_source": open_source,
            "open_status": open_status,
            "open_confidence_score": open_confidence_score,
            **extra_data,
        }

        return self.queue_kafka_event(
            event_type=KafkaEventType.OPENED,
            message_id=message_id,
            recipient_email=recipient_email,
            domain_name=domain_name,
            payload_data=payload_data,
            campaign_id=campaign_id,
        )

    def queue_email_clicked(
        self,
        message_id: str,
        recipient_email: str,
        domain_name: str,
        clicked_url: str = None,
        user_agent: str = None,
        ip_address: str = None,
        referer: str = None,
        location: str = None,
        campaign_id: str = None,
        **extra_data,
    ) -> bool:
        """Write an email-clicked event to the outbox."""
        payload_data = {
            "clicked_url": clicked_url,
            "user_agent": user_agent,
            "ip_address": ip_address,
            "referer": referer,
            "location": location,
            **extra_data,
        }

        return self.queue_kafka_event(
            event_type=KafkaEventType.CLICKED,
            message_id=message_id,
            recipient_email=recipient_email,
            domain_name=domain_name,
            payload_data=payload_data,
            campaign_id=campaign_id,
        )

    def queue_email_unsubscribed(
        self,
        message_id: str,
        recipient_email: str,
        domain_name: str,
        user_agent: str = None,
        ip_address: str = None,
        referer: str = None,
        location: str = None,
        suppression_reason: str = None,
        campaign_id: str = None,
        **extra_data,
    ) -> bool:
        """Write an email-unsubscribed event to the outbox."""
        payload_data = {
            "user_agent": user_agent,
            "ip_address": ip_address,
            "referer": referer,
            "location": location,
            "suppression_reason": suppression_reason,
            **extra_data,
        }

        return self.queue_kafka_event(
            event_type=KafkaEventType.UNSUBSCRIBED,
            message_id=message_id,
            recipient_email=recipient_email,
            domain_name=domain_name,
            payload_data=payload_data,
            campaign_id=campaign_id,
        )


# Global client instance
kafka_client = KafkaClient()
