"""
Base Pydantic models for API Keys and Channels

The projects can extend these models as needed.

Architecture:
    Customer (client_id)
        └── API Key
                └── Enabled channels
                        └── Email (config: domain_id, include_subdomains)
"""

from pydantic import BaseModel, Field, validator, model_validator
from typing import List, Optional, Dict, Any
from datetime import datetime
from enum import Enum


def migrate_owner_id(values: Dict[str, Any]) -> Dict[str, Any]:
    """
    Helper to support both user_id (deprecated) and owner_id (new standardized name).

    For API Keys, user_id refers to the owner/admin of the key (not a campaign recipient).
    The standardized name is owner_id to avoid confusion with recipient_id in other contexts.
    """
    # Accept owner_id as input, map to user_id for backward compatibility
    if values.get("owner_id") and not values.get("user_id"):
        values["user_id"] = values["owner_id"]
    # Also set owner_id from user_id for responses
    if values.get("user_id") and not values.get("owner_id"):
        values["owner_id"] = values["user_id"]
    return values


class ApiKeyPermission(str, Enum):
    """Permissions available for API Keys - the projects can extend it"""

    # Basic permissions (common to every project)
    READ_EMAIL = "read:email"
    WRITE_EMAIL = "write:email"
    READ_WEBHOOK = "read:webhook"
    WRITE_WEBHOOK = "write:webhook"
    READ_STAT = "read:stat"
    READ_SUPPRESSION = "read:suppression"
    WRITE_SUPPRESSION = "write:suppression"
    ADMIN = "admin"

    # Configuration permissions
    WRITE_CONFIG = "write:config"


class ChannelType(str, Enum):
    """Available channel types (Connect sends email only)"""

    EMAIL = "email"


# =====================================================
# CHANNEL MODELS
# =====================================================


class ChannelCreate(BaseModel):
    """Model for adding a channel to an API Key"""

    channel: ChannelType = Field(..., description="Channel type", example="email")
    config: Optional[Dict[str, Any]] = Field(
        None,
        description="Channel-specific configuration",
        example={"domain_id": 1, "include_subdomains": True},
    )
    is_active: bool = Field(True, description="Channel active", example=True)


class ChannelResponse(BaseModel):
    """Response model for an API Key channel"""

    id: int = Field(..., description="ID of the channel record")
    channel: str = Field(..., description="Channel name", example="email")
    display_name: str = Field(..., description="Display name", example="Email")
    is_active: bool = Field(..., description="Channel enabled", example=True)
    config: Optional[Dict[str, Any]] = Field(
        None,
        description="Channel configuration",
        example={"domain_id": 1, "include_subdomains": True},
    )
    created_at: Optional[datetime] = Field(None, description="Creation date")
    updated_at: Optional[datetime] = Field(None, description="Update date")


class ChannelUpdate(BaseModel):
    """Channel update model"""

    is_active: Optional[bool] = Field(None, description="Enable/disable the channel")
    config: Optional[Dict[str, Any]] = Field(None, description="New configuration")


class ChannelBulkCreate(BaseModel):
    """Model for adding several channels to an API Key"""

    channels: List[ChannelCreate] = Field(
        ...,
        description="List of channels to add",
        min_length=1,
        example=[
            {"channel": "email", "config": {"domain_id": 1}},
        ],
    )


class ChannelBulkResult(BaseModel):
    """Result of one channel addition in a bulk operation"""

    channel: str = Field(..., description="Channel name", example="email")
    success: bool = Field(..., description="Whether the operation succeeded")
    error: Optional[str] = Field(None, description="Error message if it failed")
    data: Optional[ChannelResponse] = Field(
        None, description="Channel data on success"
    )


class ChannelBulkResponse(BaseModel):
    """Response model for adding several channels"""

    total: int = Field(..., description="Total channels processed", example=3)
    success_count: int = Field(
        ..., description="Channels added successfully", example=2
    )
    error_count: int = Field(..., description="Channels that failed", example=1)
    results: List[ChannelBulkResult] = Field(..., description="Result of each channel")


class ApiKeyCreate(BaseModel):
    """API Key creation model"""

    name: str = Field(
        ...,
        description="API Key name",
        example="My API Key",
        min_length=1,
        max_length=255,
    )
    permissions: List[ApiKeyPermission] = Field(
        ..., description="List of permissions", example=["read:email", "write:email"]
    )
    expires_at: Optional[datetime] = Field(
        None, description="Expiration date (optional)", example="2025-12-31T23:59:59Z"
    )
    # Enabled channels
    channels: Optional[List[ChannelCreate]] = Field(
        None,
        description="List of channels to enable (optional at creation)",
        example=[{"channel": "email", "config": {"domain_id": 1}}],
    )

    @validator("permissions")
    def validate_permissions(cls, v):
        if not v:
            raise ValueError("At least one permission must be specified")
        return v


class ApiKeyCreateWithDomain(ApiKeyCreate):
    """
    API Key creation model with a required domain_id.
    DEPRECATED: use ApiKeyCreateAdmin and configure the email channel with a domain_id.
    Kept for compatibility.
    """

    domain_id: int = Field(
        ...,
        description="ID of the domain the API Key is created for (DEPRECATED: use channels)",
        example=1,
        gt=0,
    )


class ApiKeyCreateAdmin(ApiKeyCreate):
    """
    Administrative API Key creation model.
    Used by the /admin/create endpoint to create API Keys for customers.
    client_id and domain_id are optional (they can be linked later).
    """

    client_id: Optional[str] = Field(
        None,
        description="UUID of the customer the API Key is created for (optional, can be linked later)",
        example="550e8400-e29b-41d4-a716-446655440000",
    )
    domain_id: Optional[int] = Field(
        None,
        description="Domain ID (optional, only needed for the email channel)",
        example=1,
        gt=0,
    )


class ApiKeyUpdate(BaseModel):
    """API Key update model"""

    name: Optional[str] = Field(
        None,
        description="API Key name",
        example="Updated name",
        min_length=1,
        max_length=255,
    )
    permissions: Optional[List[ApiKeyPermission]] = Field(
        None, description="List of permissions", example=["read:email"]
    )
    is_active: Optional[bool] = Field(None, description="Active status", example=True)
    expires_at: Optional[datetime] = Field(
        None, description="Expiration date", example="2025-12-31T23:59:59Z"
    )
    domain_id: Optional[int] = Field(
        None,
        description="ID of the domain associated with the API Key",
        example=1,
        gt=0,
    )
    client_id: Optional[str] = Field(
        None,
        description="UUID of the customer to link to the API Key",
        example="550e8400-e29b-41d4-a716-446655440000",
    )


class ApiKeyResponse(BaseModel):
    """API Key response model"""

    id: int = Field(..., description="API Key ID")
    api_key_id: str = Field(
        ..., description="Unique API Key ID", example="ak_1234567890abcdef"
    )
    name: str = Field(..., description="API Key name", example="My API Key")
    permissions: List[str] = Field(
        ..., description="List of permissions", example=["read:email", "write:email"]
    )
    is_active: bool = Field(..., description="Active status", example=True)
    last_used_at: Optional[datetime] = Field(
        None, description="Last use", example="2025-01-21T16:30:00Z"
    )
    expires_at: Optional[datetime] = Field(
        None, description="Expiration date", example="2025-12-31T23:59:59Z"
    )
    created_at: datetime = Field(
        ..., description="Creation date", example="2025-01-21T16:30:00Z"
    )
    updated_at: Optional[datetime] = Field(
        None, description="Update date", example="2025-01-21T16:30:00Z"
    )
    owner_id: Optional[str] = Field(
        None, description="ID of the API Key's owner", example="user_123"
    )
    service_account_id: Optional[str] = Field(
        None,
        description="ID of the owning service account (for a service account's API Key)",
        example="sa_1234567890abcdef",
    )

    @model_validator(mode="before")
    @classmethod
    def _migrate_owner_id(cls, values):
        """Copies user_id to owner_id when needed (internal compatibility)"""
        if values.get("user_id") and not values.get("owner_id"):
            values["owner_id"] = values["user_id"]
        return values

    # client_id is the main identifier (UUID)
    # Optional for compatibility with existing API Keys without a client_id
    client_id: Optional[str] = Field(
        None,
        description="UUID of the customer that owns the API Key",
        example="550e8400-e29b-41d4-a716-446655440000",
    )
    # domain_id kept for compatibility (DEPRECATED)
    domain_id: Optional[int] = Field(
        None,
        description="Domain ID (DEPRECATED: use channels[email].config.domain_id)",
        example=1,
    )
    # Enabled channels
    channels: Optional[List[ChannelResponse]] = Field(
        None, description="List of channels enabled for this API Key"
    )
    # Simplified list of the enabled channels (names only)
    enabled_channels: Optional[List[str]] = Field(
        None,
        description="Names of the enabled channels",
        example=["email"],
    )


class ApiKeyCreateResponse(BaseModel):
    """API Key creation response model (includes the actual key)"""

    id: int = Field(..., description="API Key ID")
    api_key_id: str = Field(
        ..., description="Unique API Key ID", example="ak_1234567890abcdef"
    )
    name: str = Field(..., description="API Key name", example="My API Key")
    api_key: str = Field(
        ...,
        description="The API key (shown only at creation)",
        example="rbak_1234567890abcdef1234567890abcdef",
    )
    permissions: List[str] = Field(
        ..., description="List of permissions", example=["read:email", "write:email"]
    )
    is_active: bool = Field(..., description="Active status", example=True)
    expires_at: Optional[datetime] = Field(
        None, description="Expiration date", example="2025-12-31T23:59:59Z"
    )
    created_at: datetime = Field(
        ..., description="Creation date", example="2025-01-21T16:30:00Z"
    )
    owner_id: Optional[str] = Field(
        None, description="ID of the API Key's owner", example="user_123"
    )
    service_account_id: Optional[str] = Field(
        None,
        description="ID of the owning service account (for a service account's API Key)",
        example="sa_1234567890abcdef",
    )

    @model_validator(mode="before")
    @classmethod
    def _migrate_owner_id(cls, values):
        """Copies user_id to owner_id when needed (internal compatibility)"""
        if values.get("user_id") and not values.get("owner_id"):
            values["owner_id"] = values["user_id"]
        return values

    # Optional for compatibility with existing API Keys without a client_id
    client_id: Optional[str] = Field(
        None,
        description="UUID of the customer that owns the API Key",
        example="550e8400-e29b-41d4-a716-446655440000",
    )
    domain_id: Optional[int] = Field(
        None, description="Domain ID (DEPRECATED: use channels)", example=1
    )
    channels: Optional[List[ChannelResponse]] = Field(
        None, description="List of channels enabled for this API Key"
    )
    enabled_channels: Optional[List[str]] = Field(
        None, description="Names of the enabled channels", example=["email"]
    )


class ApiKeyListResponse(BaseModel):
    """API Key list response model"""

    api_keys: List[ApiKeyResponse] = Field(..., description="List of API Keys")
    total: int = Field(..., description="Total API Keys", example=10)
    page: int = Field(..., description="Current page", example=1)
    limit: int = Field(..., description="Items per page", example=50)


class ApiKeyFilter(BaseModel):
    """API Key filter model"""

    name: Optional[str] = Field(
        None, description="Filter by name", example="My API"
    )
    is_active: Optional[bool] = Field(
        None, description="Filter by active status", example=True
    )
    user_id: Optional[str] = Field(
        None,
        description="Filter by owner - deprecated, use owner_id",
        example="user_123",
    )
    owner_id: Optional[str] = Field(
        None,
        description="Filter by owner (standardized name)",
        example="user_123",
    )
    domain_id: Optional[int] = Field(
        None,
        description="Filter by domain (DEPRECATED: use channel_name=email)",
        example=1,
    )
    client_id: Optional[str] = Field(
        None,
        description="Filter by client_id (UUID)",
        example="550e8400-e29b-41d4-a716-446655440000",
    )
    permission: Optional[ApiKeyPermission] = Field(
        None, description="Filter by permission", example="read:email"
    )
    channel_name: Optional[ChannelType] = Field(
        None, description="Filter by enabled channel", example="email"
    )

    @model_validator(mode="before")
    @classmethod
    def _migrate_owner_id(cls, values):
        return migrate_owner_id(values)


class ApiKeyUsageStats(BaseModel):
    """API Key usage statistics model"""

    api_key_id: str = Field(
        ..., description="API Key ID", example="ak_1234567890abcdef"
    )
    name: str = Field(..., description="API Key name", example="My API Key")
    total_requests: int = Field(..., description="Total requests", example=1250)
    last_used_at: Optional[datetime] = Field(
        None, description="Last use", example="2025-01-21T16:30:00Z"
    )
    created_at: datetime = Field(
        ..., description="Creation date", example="2025-01-21T16:30:00Z"
    )
    is_active: bool = Field(..., description="Active status", example=True)
