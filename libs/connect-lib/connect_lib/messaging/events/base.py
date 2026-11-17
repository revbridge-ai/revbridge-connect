"""
Unified schema for engagement events.

This is the standard format every event must follow after normalization.
"""

import secrets
import time
from datetime import datetime, timezone
from typing import Any, Optional

from pydantic import BaseModel, Field

from .types import (
    ChannelType,
    ProviderType,
    EngagementAction,
    IdentifierType,
    Environment,
)


def create_event_id() -> str:
    """
    Generates a unique event ID.

    Format: evt_{timestamp}_{random}
    Example: evt_1706012400_x7k9m2p4
    """
    timestamp = int(time.time())
    random_part = secrets.token_hex(4)
    return f"evt_{timestamp}_{random_part}"


class ChannelInfo(BaseModel):
    """Communication channel information."""

    type: ChannelType = Field(
        ..., description="Channel type (email)"
    )
    provider: ProviderType = Field(..., description="Channel provider")
    provider_event_id: str = Field(..., description="Event ID at the provider")

    class Config:
        use_enum_values = True


class ContactInfo(BaseModel):
    """Contact information."""

    id: Optional[str] = Field(None, description="Internal contact ID (if found)")
    identifier: str = Field(..., description="Identifier (email address)")
    identifier_type: IdentifierType = Field(..., description="Identifier type")

    class Config:
        use_enum_values = True


class CampaignInfo(BaseModel):
    """Campaign information (when applicable)."""

    id: Optional[str] = Field(None, description="Campaign ID")
    journey_id: Optional[str] = Field(None, description="Journey ID")
    step_id: Optional[str] = Field(None, description="Step ID within the journey")
    message_id: Optional[str] = Field(None, description="Message ID")


class TenantInfo(BaseModel):
    """Tenant information (multi-tenancy)."""

    id: str = Field(..., description="Tenant ID")
    environment: Environment = Field(
        default=Environment.PRODUCTION, description="Environment"
    )

    class Config:
        use_enum_values = True


class GeoInfo(BaseModel):
    """Geographic information (when available)."""

    country: Optional[str] = Field(None, description="Country code (ISO 3166-1)")
    region: Optional[str] = Field(None, description="State/region")
    city: Optional[str] = Field(None, description="City")
    ip: Optional[str] = Field(None, description="IP address")


class DeviceInfo(BaseModel):
    """Device information (when available)."""

    type: Optional[str] = Field(None, description="Type (mobile, desktop, tablet)")
    os: Optional[str] = Field(None, description="Operating system")
    browser: Optional[str] = Field(None, description="Browser")
    user_agent: Optional[str] = Field(None, description="Full User-Agent")


class EngagementMetadata(BaseModel):
    """Metadata specific to each event type."""

    # Click specific
    click_url: Optional[str] = Field(None, description="Clicked URL")
    link_id: Optional[str] = Field(None, description="Short link ID")

    # Delivery failures
    error_code: Optional[str] = Field(None, description="Error code")
    error_message: Optional[str] = Field(None, description="Error message")

    # Email specific
    subject: Optional[str] = Field(None, description="Email subject")
    sender_email: Optional[str] = Field(None, description="Sender's email")
    queue_id: Optional[str] = Field(None, description="Queue ID (Postfix)")
    domain_name: Optional[str] = Field(None, description="Sending domain")

    # Geo/Device
    geo: Optional[GeoInfo] = Field(None, description="Geographic information")
    device: Optional[DeviceInfo] = Field(None, description="Device information")

    # Provider specific (extra data from the provider)
    provider_data: Optional[dict[str, Any]] = Field(
        None, description="Additional provider data"
    )


class UnifiedEngagementEvent(BaseModel):
    """
    Unified engagement event.

    This is the standard schema every event must follow after normalization.
    Any payload from any provider must be converted to this format.
    """

    # Event identification
    event_id: str = Field(
        default_factory=create_event_id, description="Unique event ID"
    )
    event_type: str = Field(default="engagement", description="Event type")
    timestamp: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
        description="Event timestamp (UTC)",
    )

    # Channel and action
    channel: ChannelInfo = Field(..., description="Channel information")
    action: EngagementAction = Field(..., description="Normalized action/status")

    # Contact
    contact: ContactInfo = Field(..., description="Contact information")

    # Campaign (optional)
    campaign: Optional[CampaignInfo] = Field(
        None, description="Campaign information"
    )

    # Metadata
    metadata: EngagementMetadata = Field(
        default_factory=EngagementMetadata, description="Event metadata"
    )

    # Tenant
    tenant: TenantInfo = Field(..., description="Tenant information")

    # Raw payload (for debugging/replay)
    raw_payload: Optional[dict[str, Any]] = Field(
        None, description="Original provider payload"
    )

    class Config:
        use_enum_values = True
        json_encoders = {
            datetime: lambda v: v.isoformat(),
        }

    def to_kafka_message(self) -> dict[str, Any]:
        """Converts to the JSON payload stored in the kafka_outbox table (the name is historical)."""
        return self.model_dump(mode="json", exclude_none=True)
