"""
Pydantic models for the RevBridge webhook system

Data models for webhooks, including:
- Webhook settings
- Webhook events
- Event payloads
- Responses and logs
"""

from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field, HttpUrl, validator


class WebhookEventType(StrEnum):
    """Event types that can trigger webhooks"""

    # Email events
    SENT = "sent"
    DELIVERED = "delivered"
    BOUNCED = "bounced"
    SOFT_BOUNCED = "soft_bounced"
    HARD_BOUNCED = "hard_bounced"
    OPENED = "opened"
    CLICKED = "clicked"
    COMPLAINT = "complaint"
    UNSUBSCRIBED = "unsubscribed"
    DEFERRED = "deferred"
    REJECTED = "rejected"
    SUPPRESSED = "suppressed"
    TEST = "test"

    # Other spellings the pipeline records (the tracking service emits "click";
    # a subscription to "clicked" receives it)
    FAILED = "failed"
    CLICK = "click"


class WebhookStatus(StrEnum):
    """Webhook processing status"""

    PENDING = "pending"
    SUCCESS = "success"
    FAILED = "failed"
    RETRYING = "retrying"


class ServiceType(StrEnum):
    """Service types supported by webhooks (Connect delivers email events only)"""
    EMAIL = "email"


class WebhookCreate(BaseModel):
    """Model to create a webhook"""

    name: str = Field(
        ..., min_length=1, max_length=255, description="Descriptive name of the webhook"
    )
    url: HttpUrl = Field(..., description="Webhook destination URL")
    events: list[WebhookEventType] = Field(
        ..., min_items=1, description="List of events that trigger this webhook"
    )
    secret_key: str | None = Field(
        None, max_length=255, description="Secret key for the HMAC signature"
    )
    is_active: bool = Field(True, description="Whether the webhook is active")
    retry_count: int = Field(
        3, ge=0, le=10, description="Number of attempts on failure"
    )
    timeout_seconds: int = Field(
        30, ge=5, le=300, description="Request timeout in seconds"
    )
    headers: dict[str, str] | None = Field(
        None, description="Custom headers sent with the webhook"
    )
    service_type: ServiceType = Field(
        ServiceType.EMAIL,
        description="Service type (always `email`)"
    )
    # Email fields (domain-based)
    domain_name: str | None = Field(
        None, description="Name of the domain associated with the webhook (email)"
    )
    domain_id: int | None = Field(
        None,
        description="ID of the domain associated with the webhook (email)",
    )

    @validator("events")
    def validate_events(cls, v):
        if not v:
            raise ValueError("At least one event must be specified")
        return list(set(v))  # Remove duplicates

    @validator("headers")
    def validate_headers(cls, v):
        if v:
            # Headers must not contain invalid values
            for key, value in v.items():
                if not isinstance(key, str) or not isinstance(value, str):
                    raise ValueError("Headers must be strings")
                if len(key) > 100 or len(value) > 1000:
                    raise ValueError("Headers are too long")
        return v


class WebhookUpdate(BaseModel):
    """Model to update a webhook"""

    name: str | None = Field(None, min_length=1, max_length=255)
    url: HttpUrl | None = None
    events: list[WebhookEventType] | None = Field(None, min_items=1)
    secret_key: str | None = Field(None, max_length=255)
    is_active: bool | None = None
    retry_count: int | None = Field(None, ge=0, le=10)
    timeout_seconds: int | None = Field(None, ge=5, le=300)
    headers: dict[str, str] | None = None

    @validator("events")
    def validate_events(cls, v):
        if v is not None and not v:
            raise ValueError("At least one event must be specified")
        return v


class WebhookResponse(BaseModel):
    """Webhook response model"""

    id: int
    webhook_id: str
    name: str
    url: str
    events: list[WebhookEventType]
    secret_key: str | None = None
    is_active: bool
    retry_count: int
    timeout_seconds: int
    headers: dict[str, str] | None = None
    created_at: datetime
    updated_at: datetime
    created_by: str | None = None
    # Service type
    service_type: ServiceType = ServiceType.EMAIL
    # Email fields (domain-based)
    domain_name: str | None = None
    domain_id: int | None = None

    class Config:
        from_attributes = True
        json_encoders = {datetime: lambda v: v.isoformat()}


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


class WebhookEventResponse(BaseModel):
    """Webhook event response model"""

    id: int
    webhook_id: int
    event_type: WebhookEventType
    message_id: str
    service_type: ServiceType = ServiceType.EMAIL
    # Email fields
    recipient_email: str | None = None
    domain_name: str | None = None
    domain_id: int | None = None
    payload: dict[str, Any]
    status: WebhookStatus
    response_code: int | None = None
    response_body: str | None = None
    error_message: str | None = None
    attempt_count: int
    next_retry_at: datetime | None = None
    sent_at: datetime | None = None
    created_at: datetime

    class Config:
        from_attributes = True
        json_encoders = {datetime: lambda v: v.isoformat()}


class WebhookLogResponse(BaseModel):
    """Webhook log response model"""

    id: int
    webhook_event_id: int
    attempt_number: int
    request_url: str
    request_headers: dict[str, str] | None = None
    request_payload: dict[str, Any] | None = None
    response_code: int | None = None
    response_headers: dict[str, str] | None = None
    response_body: str | None = None
    duration_ms: int | None = None
    error_message: str | None = None
    created_at: datetime

    class Config:
        from_attributes = True
        json_encoders = {datetime: lambda v: v.isoformat()}


class WebhookStats(BaseModel):
    """Webhook statistics model"""

    webhook_id: int
    webhook_name: str
    total_events: int
    successful_events: int
    failed_events: int
    pending_events: int
    retrying_events: int
    success_rate_percent: float
    avg_response_time_ms: float | None = None
    last_event_at: datetime | str | None = None

    class Config:
        json_encoders = {
            datetime: lambda v: v.isoformat() if isinstance(v, datetime) else v
        }


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


class WebhookTestRequest(BaseModel):
    """Webhook test model"""

    webhook_id: int
    test_payload: dict[str, Any] | None = None


class WebhookFilter(BaseModel):
    """Webhook search filter model"""

    is_active: bool | None = None
    event_type: WebhookEventType | None = None
    service_type: ServiceType | None = None
    created_after: datetime | None = None
    created_before: datetime | None = None
    search: str | None = None  # Search by name or URL


class WebhookEventFilter(BaseModel):
    """Webhook event search filter model"""

    webhook_id: int | None = None
    event_type: WebhookEventType | None = None
    status: WebhookStatus | None = None
    message_id: str | None = None
    created_after: datetime | None = None
    created_before: datetime | None = None
    limit: int = Field(100, ge=1, le=1000)
    offset: int = Field(0, ge=0)


# Utility function that builds the payload for an event type
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

    return payload_class(**kwargs)


# Utility function re-exported as part of mail_api.models
from mail_api.shared.utils import generate_webhook_signature  # noqa: F401, E402
