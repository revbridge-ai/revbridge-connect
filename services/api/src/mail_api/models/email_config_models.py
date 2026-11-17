"""
Pydantic models for per customer/domain email configurations

Data models for email configurations, including:
- SMTP settings
- Tracking settings
- Rate limiting settings
"""

from datetime import datetime
from typing import Any

from pydantic import BaseModel, EmailStr, Field, validator


class EmailConfigCreate(BaseModel):
    """Model to create an email configuration"""

    name: str = Field(..., min_length=1, max_length=255, description="Descriptive name of the configuration")

    # Explicit target. Without it the config is created on the caller's primary
    # domain (email channel > legacy), which is the wrong target when a key is
    # being set up for an additional domain.
    domain_id: int | None = Field(
        None,
        description="Target domain of the config; must be authorized for the key (primary, legacy or api_key_domains). Default: the caller's primary domain.",
    )

    # SMTP
    smtp_host: str = Field(..., min_length=1, max_length=255, description="SMTP server host")
    smtp_port: int = Field(587, ge=1, le=65535, description="SMTP port")
    smtp_secure: bool = Field(False, description="Use SSL/TLS")
    smtp_user: str | None = Field(None, max_length=255, description="SMTP user")
    smtp_password: str | None = Field(None, max_length=255, description="SMTP password")
    smtp_base_port: int = Field(587, ge=1, le=65535, description="Base port for the dynamic port calculation")
    smtp_timeout: int = Field(30, ge=1, le=300, description="SMTP connection timeout in seconds")
    envelope_prefix: str | None = Field(None, max_length=64, description="rbm<id> prefix for an SPF-aligned Return-Path; decouples the identity from smtp_host (allows a shared smtp_host)")

    # Tracking
    tracking_enabled: bool = Field(True, description="Enable tracking")
    inject_pixel: bool = Field(True, description="Inject the tracking pixel into emails")
    rewrite_links: bool = Field(True, description="Rewrite links for click tracking")
    pixel_tracking_url: str | None = Field(None, max_length=500, description="Base URL of the tracking pixel")
    click_tracking_url: str | None = Field(None, max_length=500, description="Base URL for click tracking")
    tracking_domain: str | None = Field(None, max_length=255, description="Tracking domain")

    # From/Reply
    default_from_name: str | None = Field(None, max_length=255, description="Default sender name")
    default_from_email: EmailStr | None = Field(None, description="Default sender email")
    default_reply_to: EmailStr | None = Field(None, description="Default reply-to email")

    # Bounce/Complaint
    bounce_email: EmailStr | None = Field(None, description="Email that receives bounces")
    complaint_email: EmailStr | None = Field(None, description="Email that receives complaints")

    # Shadow Email
    shadow_email: EmailStr | None = Field(None, description="Email for a hidden audit copy")

    # Rate Limiting
    rate_limit_per_minute: int = Field(100, ge=1, description="Email limit per minute")
    rate_limit_per_hour: int = Field(1000, ge=1, description="Email limit per hour")
    rate_limit_per_day: int = Field(10000, ge=1, description="Email limit per day")

    # Retry
    max_retries: int = Field(3, ge=0, le=10, description="Maximum number of attempts")
    retry_delay_seconds: int = Field(60, ge=1, description="Delay between attempts in seconds")

    # Default webhook
    default_webhook_url: str | None = Field(None, max_length=500, description="Default webhook URL")
    default_webhook_secret: str | None = Field(None, max_length=255, description="Default webhook secret key")

    # Additional settings
    additional_config: dict[str, Any] | None = Field(None, description="Additional settings as JSON")

    # Metadata
    is_default: bool = Field(False, description="Default configuration for the domain")
    priority: int = Field(0, ge=0, description="Priority (higher = preferred)")
    notes: str | None = Field(None, description="Notes about the configuration")

    @validator('smtp_port')
    def validate_smtp_port(cls, v, values):
        """Validates common SMTP ports"""
        common_ports = [25, 465, 587, 2525]
        if v not in common_ports:
            # Other ports are allowed
            pass
        return v

    @validator('smtp_secure')
    def validate_smtp_secure(cls, v, values):
        """Checks that secure matches the port"""
        if 'smtp_port' in values:
            port = values['smtp_port']
            if port == 465 and not v:
                # Port 465 usually requires SSL
                pass
        return v


class EmailConfigUpdate(BaseModel):
    """Model to update an email configuration"""

    name: str | None = Field(None, min_length=1, max_length=255)

    # SMTP
    smtp_host: str | None = Field(None, min_length=1, max_length=255)
    smtp_port: int | None = Field(None, ge=1, le=65535)
    smtp_secure: bool | None = None
    smtp_user: str | None = Field(None, max_length=255)
    smtp_password: str | None = Field(None, max_length=255)
    smtp_base_port: int | None = Field(None, ge=1, le=65535)
    smtp_timeout: int | None = Field(None, ge=1, le=300)
    envelope_prefix: str | None = Field(None, max_length=64, description="rbm<id> prefix for an SPF-aligned Return-Path (decoupled from smtp_host)")

    # Tracking
    tracking_enabled: bool | None = None
    inject_pixel: bool | None = None
    rewrite_links: bool | None = None
    pixel_tracking_url: str | None = Field(None, max_length=500)
    click_tracking_url: str | None = Field(None, max_length=500)
    tracking_domain: str | None = Field(None, max_length=255)

    # From/Reply
    default_from_name: str | None = Field(None, max_length=255)
    default_from_email: EmailStr | None = None
    default_reply_to: EmailStr | None = None

    # Bounce/Complaint
    bounce_email: EmailStr | None = None
    complaint_email: EmailStr | None = None

    # Shadow Email
    shadow_email: EmailStr | None = None

    # Rate Limiting
    rate_limit_per_minute: int | None = Field(None, ge=1)
    rate_limit_per_hour: int | None = Field(None, ge=1)
    rate_limit_per_day: int | None = Field(None, ge=1)

    # Retry
    max_retries: int | None = Field(None, ge=0, le=10)
    retry_delay_seconds: int | None = Field(None, ge=1)

    # Default webhook
    default_webhook_url: str | None = Field(None, max_length=500)
    default_webhook_secret: str | None = Field(None, max_length=255)

    # Additional settings
    additional_config: dict[str, Any] | None = None

    # Metadata
    is_active: bool | None = None
    is_default: bool | None = None
    priority: int | None = Field(None, ge=0)
    notes: str | None = None


class EmailConfigResponse(BaseModel):
    """Email configuration response model"""

    id: int
    config_id: str
    domain_id: int
    name: str

    # SMTP
    smtp_host: str
    smtp_port: int
    smtp_secure: bool
    smtp_user: str | None
    smtp_base_port: int
    smtp_timeout: int

    # Tracking
    tracking_enabled: bool
    inject_pixel: bool
    rewrite_links: bool
    pixel_tracking_url: str | None
    click_tracking_url: str | None
    tracking_domain: str | None

    # From/Reply
    default_from_name: str | None
    default_from_email: str | None
    default_reply_to: str | None

    # Bounce/Complaint
    bounce_email: str | None
    complaint_email: str | None

    # Shadow Email
    shadow_email: str | None

    # Rate Limiting
    rate_limit_per_minute: int
    rate_limit_per_hour: int
    rate_limit_per_day: int

    # Retry
    max_retries: int
    retry_delay_seconds: int

    # Default webhook
    default_webhook_url: str | None
    default_webhook_secret: str | None

    # Additional settings
    additional_config: dict[str, Any] | None

    # Metadata
    is_active: bool
    is_default: bool
    priority: int
    notes: str | None
    created_by: str | None
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


class EmailConfigFilter(BaseModel):
    """Configuration search filters"""

    domain_id: int | None = None
    is_active: bool | None = None
    is_default: bool | None = None
    smtp_host: str | None = None
    tracking_enabled: bool | None = None
