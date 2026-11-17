"""
Models for the Outbox pattern.
"""

from datetime import datetime, timezone
from enum import Enum
from typing import Any, Optional

from pydantic import BaseModel, Field

from ..events import UnifiedEngagementEvent


class OutboxStatus(str, Enum):
    """Status of the message in the outbox."""

    PENDING = "pending"
    PROCESSING = "processing"
    SENT = "sent"
    FAILED = "failed"


class OutboxMessage(BaseModel):
    """
    A row of the kafka_outbox table.

    An event stored in the outbox; in RevBridge Connect it is read by the webhook-dispatcher.
    """

    id: Optional[int] = Field(None, description="Auto-increment ID")

    # Identification
    service_type: str = Field(..., description="Service type (email)")
    event_type: str = Field(..., description="Event type (sent, delivered, etc)")
    message_id: str = Field(..., description="Unique message ID")

    # Destination (email)
    rcpt: Optional[str] = Field(None, description="Recipient (email)")
    domain_name: Optional[str] = Field(None, description="Sending domain")

    # Customer
    client_id: Optional[str] = Field(None, description="Customer ID")

    # Event payload
    payload: dict[str, Any] = Field(..., description="The event's JSON payload")

    # Processing status
    status: OutboxStatus = Field(default=OutboxStatus.PENDING, description="Status")
    retry_count: int = Field(default=0, description="Number of attempts")
    error_message: Optional[str] = Field(None, description="Error message")

    # Timestamps
    created_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
        description="Creation date",
    )
    updated_at: Optional[datetime] = Field(None, description="Update date")
    sent_at: Optional[datetime] = Field(None, description="Send date")

    class Config:
        use_enum_values = True

    @classmethod
    def from_event(
        cls,
        event: UnifiedEngagementEvent,
        include_raw_payload: bool = False,
    ) -> "OutboxMessage":
        """
        Creates an OutboxMessage from a UnifiedEngagementEvent.

        Args:
            event: Normalized event
            include_raw_payload: Whether to include raw_payload
        """
        # Determine the service_type
        service_type = event.channel.type

        # Prepare the payload
        payload = event.to_kafka_message()
        if not include_raw_payload:
            payload.pop("raw_payload", None)

        # Contact fields
        identifier = event.contact.identifier
        identifier_type = event.contact.identifier_type

        return cls(
            service_type=service_type,
            event_type=event.action,
            message_id=event.channel.provider_event_id,
            rcpt=identifier if identifier_type == "email" else None,
            domain_name=event.metadata.domain_name if event.metadata else None,
            client_id=event.tenant.id,
            payload=payload,
        )

    def to_dict(self) -> dict[str, Any]:
        """Converts to a dict for the database insert."""
        return {
            "service_type": self.service_type,
            "event_type": self.event_type,
            "message_id": self.message_id,
            "rcpt": self.rcpt,
            "domain_name": self.domain_name,
            "client_id": self.client_id,
            "payload": self.payload,
            "status": self.status,
            "retry_count": self.retry_count,
            "error_message": self.error_message,
            "created_at": self.created_at,
        }
