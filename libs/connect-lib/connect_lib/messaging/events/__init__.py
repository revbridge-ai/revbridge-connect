"""
Normalized events module.

Defines the schema for email engagement events.
"""

from .types import (
    ChannelType,
    ProviderType,
    EngagementAction,
    IdentifierType,
    Environment,
)
from .base import (
    UnifiedEngagementEvent,
    ChannelInfo,
    ContactInfo,
    CampaignInfo,
    TenantInfo,
    EngagementMetadata,
    GeoInfo,
    DeviceInfo,
    create_event_id,
)

__all__ = [
    # Types/Enums
    "ChannelType",
    "ProviderType",
    "EngagementAction",
    "IdentifierType",
    "Environment",
    # Models
    "UnifiedEngagementEvent",
    "ChannelInfo",
    "ContactInfo",
    "CampaignInfo",
    "TenantInfo",
    "EngagementMetadata",
    "GeoInfo",
    "DeviceInfo",
    # Utils
    "create_event_id",
]
