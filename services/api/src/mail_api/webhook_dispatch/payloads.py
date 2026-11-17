"""Email webhook payloads.

The payload classes and `create_event_payload` build the body the dispatcher sends.
`create_event_payload` re-injects `event_type` and derives `bounce_type` when it is missing.

`EVENT_ALIASES` maps click <-> clicked (the tracking service emits "click" and webhooks
subscribe to "clicked") and lets a `bounced` subscription receive hard and soft bounces.
"""

from datetime import datetime
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


class WebhookEventType(str, Enum):
    """Event types that can trigger webhooks"""

    SENT = "sent"
    FAILED = "failed"
    DELIVERED = "delivered"
    CLICK = "click"

    # Email events
    BOUNCED = "bounced"
    SOFT_BOUNCED = "soft_bounced"
    HARD_BOUNCED = "hard_bounced"
    OPENED = "opened"
    CLICKED = "clicked"  # Alias for CLICK
    COMPLAINT = "complaint"
    UNSUBSCRIBED = "unsubscribed"
    DEFERRED = "deferred"
    REJECTED = "rejected"
    SUPPRESSED = "suppressed"
    TEST = "test"


# Event names that must match each other's subscriptions.
EVENT_ALIASES: dict[str, list[str]] = {
    "click": ["click", "clicked"],
    "clicked": ["click", "clicked"],
    # `bounced` is the umbrella subscription (the panel offers only it): a webhook subscribed to
    # it also receives hard and soft bounces, whose payload keeps the specific event_type
    "hard_bounced": ["hard_bounced", "bounced"],
    "soft_bounced": ["soft_bounced", "bounced"],
}


def event_type_variants(event_type: "WebhookEventType | str") -> list[str]:
    value = event_type.value if isinstance(event_type, WebhookEventType) else event_type
    return EVENT_ALIASES.get(value, [value])


class ServiceType(str, Enum):
    """Service types supported by webhooks (Connect delivers email events only)"""
    EMAIL = "email"


class WebhookEventPayload(BaseModel):
    """Base payload for webhook events"""

    event_type: WebhookEventType
    message_id: str
    timestamp: datetime = Field(default_factory=datetime.utcnow)
    # Service type
    service_type: ServiceType = ServiceType.EMAIL
    # Email fields
    recipient_email: str | None = None
    domain_name: str | None = None
    domain_id: int | None = None
    # Customer
    client_id: str | None = None
    # Additional data
    data: dict[str, Any] = Field(default_factory=dict)
    message_headers: dict[str, str] | None = Field(
        None, description="Headers of the original email message"
    )

    class Config:
        json_encoders = {datetime: lambda v: v.isoformat()}


class EmailSentPayload(WebhookEventPayload):
    """Payload for the email sent event"""

    event_type: WebhookEventType = WebhookEventType.SENT
    subject: str | None = None
    sender_email: str | None = None
    recipient_count: int = 1


class EmailDeliveredPayload(WebhookEventPayload):
    """Payload for the email delivered event"""

    event_type: WebhookEventType = WebhookEventType.DELIVERED
    delivery_time: datetime | None = None
    server_response: str | None = None


class EmailBouncedPayload(WebhookEventPayload):
    """Payload for the email bounced event"""

    event_type: WebhookEventType = WebhookEventType.BOUNCED
    bounce_type: str = Field(
        ..., description="Bounce type: hard, soft, complaint, etc"
    )
    bounce_reason: str | None = None
    bounce_code: str | None = None
    server_response: str | None = None


class EmailOpenedPayload(WebhookEventPayload):
    """Payload for the email opened event"""

    event_type: WebhookEventType = WebhookEventType.OPENED
    user_agent: str | None = None
    ip_address: str | None = None
    location: dict[str, str | None] | None = None


class EmailClickedPayload(WebhookEventPayload):
    """Payload for the link clicked event"""

    event_type: WebhookEventType = WebhookEventType.CLICKED
    clicked_url: str
    user_agent: str | None = None
    ip_address: str | None = None
    location: dict[str, str | None] | None = None


class EmailComplaintPayload(WebhookEventPayload):
    """Payload for the complaint event"""

    event_type: WebhookEventType = WebhookEventType.COMPLAINT
    complaint_type: str | None = None
    complaint_feedback: str | None = None
    source_ip: str | None = None


class EmailUnsubscribedPayload(WebhookEventPayload):
    """Payload for the unsubscribe event"""

    event_type: WebhookEventType = WebhookEventType.UNSUBSCRIBED
    unsubscribe_reason: str | None = None
    unsubscribe_url: str | None = None


class WebhookQueueMessage(BaseModel):
    """Message model for the Redis queue"""

    webhook_id: int
    event_type: WebhookEventType
    message_id: str
    payload: dict[str, Any]
    attempt: int = 1
    max_attempts: int = 3
    created_at: datetime = Field(default_factory=datetime.utcnow)
    scheduled_at: datetime | None = None

    class Config:
        json_encoders = {datetime: lambda v: v.isoformat()}


def create_event_payload(event_type: WebhookEventType, **kwargs) -> WebhookEventPayload:
    """Builds the payload class that matches the event type"""
    payload_classes = {
        WebhookEventType.SENT: EmailSentPayload,
        WebhookEventType.DELIVERED: EmailDeliveredPayload,
        WebhookEventType.BOUNCED: EmailBouncedPayload,
        WebhookEventType.SOFT_BOUNCED: EmailBouncedPayload,
        WebhookEventType.HARD_BOUNCED: EmailBouncedPayload,
        WebhookEventType.OPENED: EmailOpenedPayload,
        WebhookEventType.CLICKED: EmailClickedPayload,
        WebhookEventType.COMPLAINT: EmailComplaintPayload,
        WebhookEventType.UNSUBSCRIBED: EmailUnsubscribedPayload,
        WebhookEventType.TEST: WebhookEventPayload,
    }

    payload_class = payload_classes.get(event_type, WebhookEventPayload)

    # Pull the message headers out of the general data, if present
    message_headers = kwargs.pop("message_headers", None)
    if message_headers is None and "data" in kwargs:
        # Try to take the headers from the general data
        data = kwargs.get("data", {})
        if isinstance(data, dict) and "message_headers" in data:
            message_headers = data.pop("message_headers")
            kwargs["data"] = data

    if message_headers:
        kwargs["message_headers"] = message_headers

    # Drop None values so the model defaults apply
    kwargs = {k: v for k, v in kwargs.items() if v is not None}

    # event_type is a named parameter of create_event_payload, so it is not in kwargs.
    # The subclasses (EmailSentPayload, etc.) redeclare event_type with a default, but the
    # base WebhookEventPayload requires event_type without a default: without re-injecting
    # it, TEST and any event_type that falls back to the base class (FAILED, READ, CREATED, …)
    # would raise `ValidationError: event_type Field required`.
    kwargs["event_type"] = event_type

    # Bounce payloads require `bounce_type`, but the upstream pipeline (events updater)
    # does not always fill it in; when it is missing, a ValidationError would drop the
    # whole webhook. Deriving it from event_type gives a useful value instead.
    if payload_class is EmailBouncedPayload and "bounce_type" not in kwargs:
        kwargs["bounce_type"] = {
            WebhookEventType.HARD_BOUNCED: "hard",
            WebhookEventType.SOFT_BOUNCED: "soft",
            WebhookEventType.BOUNCED: "hard",
        }.get(event_type, "unknown")

    return payload_class(**kwargs)
