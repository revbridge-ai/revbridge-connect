"""
Pydantic models for email data validation
"""

import math
import re
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, Field, field_validator


class EmailAddress(BaseModel):
    """Email address model"""

    name: str | None = Field(default="", description="Recipient/sender name")
    address: str = Field(..., description="Email address")

    @field_validator("address")
    def validate_email_address(cls, v: str) -> str:
        """Validates the email address format"""
        email_regex = r"^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$"
        if not re.match(email_regex, v):
            raise ValueError("Invalid email format")
        return v.lower()


class EmailContent(BaseModel):
    """Email content model"""

    type: str = Field(
        ...,
        description="Content type (text/plain, text/html)",
    )
    value: str = Field(..., description="Email content")

    @field_validator("type")
    def validate_content_type(cls, v: str) -> str:
        """Validates the content type"""
        allowed_types = ["text/plain", "text/html"]
        if v not in allowed_types:
            raise ValueError(
                f"Content type must be one of: {allowed_types}"
            )
        return v


def coerce_substitution_value(v: str | int | float | bool | None) -> str | None:
    """JSON scalar → str (merge tags contract).

    str stays as is (legacy, byte-identical); bool → "true"/"false" (the JSON
    literal, same as String(true) in JavaScript); int/float → Python str()
    (150 → "150", 4.5 → "4.5", as in the fixture); None stays None and each
    policy decides (legacy: ""; v2: missing key → fallback). NaN/±inf are
    rejected (they would render as "nan"/"inf" in the email)."""
    if v is None or isinstance(v, str):
        return v
    if isinstance(v, bool):  # before int: bool is a subclass of int
        return "true" if v else "false"
    if isinstance(v, float) and not math.isfinite(v):
        raise ValueError("substitutions does not accept non-finite floats (NaN/Infinity)")
    return str(v)


class Personalization(BaseModel):
    """Email personalization model"""

    to: list[EmailAddress] = Field(..., description="List of recipients")
    substitutions: dict[str, str | int | float | bool | None] = Field(
        default_factory=dict,
        description=(
            "Variable substitutions. Accepts any JSON scalar; the value is "
            "converted to a string (null = missing)."
        ),
    )
    headers: dict[str, str] = Field(
        default_factory=dict, description="Custom headers"
    )
    token_policy: Literal["legacy", "v2"] = Field(
        default="legacy",
        description=(
            "{{token}} substitution rule. 'legacy' (default): the original "
            "behavior. 'v2': merge tags with an inline fallback "
            "{{user.x|default}}, user.*/event.*, a missing value becomes empty; "
            "{{unsub_link}} is never touched."
        ),
    )

    @field_validator("substitutions", mode="after")
    def coerce_substitutions(
        cls, v: dict[str, str | int | float | bool | None]
    ) -> dict[str, str | None]:
        return {k: coerce_substitution_value(val) for k, val in v.items()}


class EmailStatus(StrEnum):
    """Email status"""

    PENDING = "pending"
    QUEUED = "queued"
    SENDING = "sending"
    SENT = "sent"
    FAILED = "failed"
    BOUNCED = "bounced"
    DELIVERED = "delivered"
    # Nothing was queued: every recipient was on this sender's suppression
    # list. 200 response (the request was valid), with recipients_count=0:
    # not a caller error and not retryable.
    SUPPRESSED = "suppressed"


class SendEmailRequest(BaseModel):
    """Send email request model"""

    personalizations: list[Personalization] = Field(
        ..., description="List of personalizations"
    )
    headers: dict[str, str] = Field(
        default_factory=dict,
        description="Global headers",
    )
    from_: EmailAddress = Field(..., alias="from", description="Sender")
    reply_to: EmailAddress | None = Field(
        default=None,
        description=(
            "Reply address (Reply-To). If not given, the domain configuration's "
            "default_reply_to is used."
        ),
    )
    subject: str = Field(..., min_length=1, description="Email subject")
    content: list[EmailContent] = Field(..., description="Email content")
    priority: int = Field(
        default=5, ge=1, le=10, description="Email priority (1-10)"
    )
    server: str | None = Field(
        default=None,
        description=(
            "Specific SMTP server for the send "
            "(e.g. server-1, server-2) - optional"
        ),
    )
    scheduled_at: str | None = Field(
        default=None,
        description=(
            "Scheduled send date/time in ISO8601 format "
            "(e.g. 2025-01-21T16:30:00Z). If it is in the future, the email "
            "goes to the scheduled queue."
        ),
    )
    # Kept only for payload compatibility: the send is always attributed to the
    # authenticated client (client_id of the token).
    customer_id: str | None = Field(
        default=None,
        deprecated=True,
        description=(
            "Ignored. Accepted for compatibility; the send is always attributed "
            "to the authenticated client (client_id of the token)."
        ),
    )

    @field_validator("personalizations")
    def validate_personalizations(
        cls, v: list[Personalization]
    ) -> list[Personalization]:
        """Requires at least one personalization"""
        if not v:
            raise ValueError("At least one personalization is required")
        return v

    @field_validator("content")
    def validate_content(cls, v: list[EmailContent]) -> list[EmailContent]:
        """Requires at least one content type"""
        if not v:
            raise ValueError("At least one content type is required")

        # Require at least text/plain or text/html
        content_types = [c.type for c in v]
        if "text/plain" not in content_types and "text/html" not in content_types:
            raise ValueError("At least text/plain or text/html is required")

        return v

    @field_validator("server")
    def validate_server(cls, v: str | None) -> str | None:
        """Validates the server format"""
        if v is None:
            return v

        # Valid servers: server-X where X is any number
        import re

        server_pattern = r"^server-\d+$"
        if not re.match(server_pattern, v):
            raise ValueError(
                "Server must use the 'server-X' format where X is a number "
                "(e.g. server-1, server-2, server-10)"
            )

        return v

    @field_validator("scheduled_at")
    def validate_scheduled_at(cls, v: str | None) -> str | None:
        """Validates the basic scheduled_at format (ISO8601)"""
        if v is None:
            return v

        # Accepts ISO8601 dates, with or without a trailing 'Z'
        import datetime

        try:
            # Replace 'Z' with '+00:00' for fromisoformat
            datetime.datetime.fromisoformat(v.replace("Z", "+00:00"))
        except Exception:
            raise ValueError(
                "scheduled_at must be a valid ISO8601 date "
                "(e.g. 2025-01-21T16:30:00Z)"
            ) from None

        return v


class EmailResponse(BaseModel):
    """Send email response model"""

    message_id: str = Field(..., description="Unique message ID")
    status: EmailStatus = Field(..., description="Email status")
    domain_name: str = Field(..., description="Domain name")
    queue: str = Field(..., description="Queue name")
    timestamp: str = Field(..., description="Send timestamp")
    recipients_count: int = Field(..., description="Number of recipients")
    scheduled_count: int | None = Field(
        None,
        description=(
            "Recipients deferred to tomorrow's window by the volume governor "
            "(domain ramp-up). Included in recipients_count; they will be "
            "sent automatically, nothing to retry."
        ),
    )
