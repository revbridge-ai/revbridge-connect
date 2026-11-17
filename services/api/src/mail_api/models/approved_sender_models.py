"""
Pydantic models for Approved Senders
"""

from datetime import datetime

from pydantic import BaseModel, Field, validator


class ApprovedSenderCreate(BaseModel):
    """Model to create an Approved Sender

    Note: principal_domain_id is taken automatically from the authenticated
    user's JWT
    """

    sender_email: str = Field(
        ...,
        description="Full sender email (e.g. newsletter@example.com)",
        example="newsletter@example.com",
        min_length=1,
        max_length=255,
    )
    is_active: bool = Field(
        True,
        description="Whether the sender is active",
        example=True,
    )

    @validator("sender_email")
    def validate_sender_email(cls, v):
        if not v or not v.strip():
            raise ValueError("sender_email cannot be empty")
        # Basic email format check
        if "@" not in v:
            raise ValueError(
                "sender_email must be a valid email address (e.g. user@domain.com)"
            )
        # Strip spaces and normalize
        return v.strip().lower()


class ApprovedSenderUpdate(BaseModel):
    """Model to update an Approved Sender

    Note: principal_domain_id cannot be changed; it is always the
    domain_id of the user's JWT
    """

    sender_email: str | None = Field(
        None,
        description="Full sender email (e.g. newsletter@example.com)",
        example="newsletter@example.com",
        min_length=1,
        max_length=255,
    )
    is_active: bool | None = Field(
        None,
        description="Whether the sender is active",
        example=True,
    )

    @validator("sender_email")
    def validate_sender_email(cls, v):
        if v is not None:
            if not v or not v.strip():
                raise ValueError("sender_email cannot be empty")
            # Basic email format check
            if "@" not in v:
                raise ValueError(
                    "sender_email must be a valid email address "
                    "(e.g. user@domain.com)"
                )
            return v.strip().lower()
        return v


class ApprovedSenderResponse(BaseModel):
    """Approved Sender response model"""

    id: int = Field(..., description="Approved Sender ID")
    sender_email: str = Field(
        ...,
        description="Full sender email",
        example="newsletter@example.com",
    )
    principal_domain_id: int = Field(
        ...,
        description="ID of the authorized main domain",
        example=1,
    )
    is_active: bool = Field(
        ..., description="Whether the sender is active", example=True
    )
    created_at: datetime = Field(
        ..., description="Creation date", example="2025-01-21T16:30:00Z"
    )
    updated_at: datetime = Field(
        ..., description="Last update date", example="2025-01-21T16:30:00Z"
    )

    class Config:
        from_attributes = True


class ApprovedSenderListResponse(BaseModel):
    """Response model for the Approved Sender list"""

    approved_senders: list[ApprovedSenderResponse] = Field(
        ..., description="List of Approved Senders"
    )
    total: int = Field(
        ..., description="Total Approved Senders", example=10
    )
    page: int = Field(..., description="Current page", example=1)
    limit: int = Field(..., description="Items per page", example=50)


class ApprovedSenderFilter(BaseModel):
    """Approved Sender filter model"""

    sender_email: str | None = Field(
        None,
        description="Filter by sender_email",
        example="newsletter@example.com",
    )
    principal_domain_id: int | None = Field(
        None, description="Filter by main domain", example=1
    )
    is_active: bool | None = Field(
        None, description="Filter by active status", example=True
    )
