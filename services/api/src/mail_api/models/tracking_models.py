"""
Pydantic models for tracking

Every model related to email tracking:
- Tracking statistics
- Suppressions
- Deliverability
- Tracking events
"""

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, Field


class TrackingEventType(StrEnum):
    """Tracking event types"""
    OPEN = "open"
    CLICK = "click"
    UNSUBSCRIBE = "unsubscribe"

class TrackingStats(BaseModel):
    """Tracking statistics"""
    total_opens: int = Field(..., description="Total opens")
    total_clicks: int = Field(..., description="Total clicks")
    total_unsubscribes: int = Field(..., description="Total unsubscribes")
    unique_opens: int = Field(..., description="Unique opens")
    unique_clicks: int = Field(..., description="Unique clicks")
    open_rate: float = Field(..., description="Open rate")
    click_rate: float = Field(..., description="Click rate")
    unsubscribe_rate: float = Field(..., description="Unsubscribe rate")
    click_to_open_rate: float = Field(..., description="Click-to-open rate")

class TrackingEvent(BaseModel):
    """Tracking event"""
    id: int = Field(..., description="Event ID")
    message_id: str = Field(..., description="Message ID")
    recipient: str = Field(..., description="Recipient")
    event_type: TrackingEventType = Field(..., description="Event type")
    timestamp: datetime = Field(..., description="Event date and time")
    ip_address: str | None = Field(None, description="IP address")
    user_agent: str | None = Field(None, description="User Agent")
    referer: str | None = Field(None, description="Referer")
    url: str | None = Field(None, description="Clicked URL")
    domain_id: int = Field(..., description="Domain ID")

class TrackingEventResponse(BaseModel):
    """Tracking event response"""
    success: bool = Field(..., description="Whether the operation succeeded")
    message: str = Field(..., description="Response message")
    event_id: int | None = Field(None, description="ID of the created event")

class TrackingFilter(BaseModel):
    """Filters to query tracking events"""
    message_id: str | None = Field(None, description="Filter by message ID")
    recipient: str | None = Field(None, description="Filter by recipient")
    event_type: TrackingEventType | None = Field(None, description="Filter by event type")
    start_date: datetime | None = Field(None, description="Start date")
    end_date: datetime | None = Field(None, description="End date")
    limit: int = Field(100, description="Result limit")
    offset: int = Field(0, description="Result offset")

class TrackingEventListResponse(BaseModel):
    """Tracking event list response"""
    events: list[TrackingEvent] = Field(..., description="List of events")
    total: int = Field(..., description="Total events")
    page: int = Field(..., description="Current page")
    per_page: int = Field(..., description="Events per page")
    total_pages: int = Field(..., description="Total pages")

class SuppressionEntry(BaseModel):
    """Suppression entry"""
    id: int = Field(..., description="Suppression ID")
    email: str = Field(..., description="Suppressed email")
    reason: str = Field(..., description="Suppression reason")
    created_at: datetime = Field(..., description="Creation date")
    updated_at: datetime = Field(..., description="Last update date")
    # RevBridge Connect: the panel shows the type (bounce, unsubscribe, complaint, manual)
    suppression_type: str | None = Field(None, description="bounce | complaint | unsubscribe | manual")
    domain_name: str | None = Field(None, description="Domain the suppression was recorded under")
    is_active: bool | None = Field(None, description="Whether the suppression is active")

class SuppressionCreate(BaseModel):
    """Suppression creation"""
    email: str = Field(..., description="Email to suppress")
    reason: str = Field(..., description="Suppression reason")
    # RevBridge Connect: the domain is always the token's (the value sent is ignored)
    domain_name: str | None = Field(None, description="Ignored: the domain comes from the token")
    suppression_type: str = Field("manual", description="Suppression type (default: manual)")

class SuppressionResponse(BaseModel):
    """Suppression response"""
    success: bool = Field(..., description="Whether the operation succeeded")
    message: str = Field(..., description="Response message")
    suppression: SuppressionEntry | None = Field(None, description="Created suppression")

class SuppressionListResponse(BaseModel):
    """Suppression list response"""
    suppressions: list[SuppressionEntry] = Field(..., description="List of suppressions")
    total: int = Field(..., description="Total suppressions")
    page: int = Field(..., description="Current page")
    per_page: int = Field(..., description="Suppressions per page")
    total_pages: int = Field(..., description="Total pages")

class DeliverabilityMetrics(BaseModel):
    """Deliverability metrics"""
    total_sent: int = Field(..., description="Total emails sent")
    total_delivered: int = Field(..., description="Total emails delivered")
    total_bounced: int = Field(..., description="Total bounces")
    total_complained: int = Field(..., description="Total complaints")
    delivery_rate: float = Field(..., description="Delivery rate")
    bounce_rate: float = Field(..., description="Bounce rate")
    complaint_rate: float = Field(..., description="Complaint rate")
    open_rate: float = Field(..., description="Open rate")
    click_rate: float = Field(..., description="Click rate")
    unsubscribe_rate: float = Field(..., description="Unsubscribe rate")

class DeliverabilityResponse(BaseModel):
    """Deliverability metrics response"""
    metrics: DeliverabilityMetrics = Field(..., description="Deliverability metrics")
    period: str = Field(..., description="Metrics period")
    generated_at: datetime = Field(..., description="Generation date")

class HealthCheckResponse(BaseModel):
    """Health check response"""
    status: str = Field(..., description="Service status")
    service: str = Field(..., description="Service name")
    timestamp: datetime = Field(..., description="Check timestamp")
    version: str = Field(..., description="Service version")
    uptime: float | None = Field(None, description="Uptime in seconds")
    dependencies: dict[str, str] | None = Field(None, description="Dependency status")
