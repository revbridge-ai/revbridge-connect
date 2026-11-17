"""
Normalized engagement events and the outbox format.

This module provides:
- Normalized events (UnifiedEngagementEvent)
- OutboxMessage: the event in the format of a kafka_outbox table row

In RevBridge Connect the services store the events in kafka_outbox (the name is historical)
and the webhook-dispatcher reads them from there; there is no Kafka broker.

Usage example:

    from connect_lib.messaging import OutboxMessage, UnifiedEngagementEvent

    outbox_message = OutboxMessage.from_event(event, include_raw_payload=True)
"""

from .events import (
    UnifiedEngagementEvent,
    ChannelType,
    ProviderType,
    EngagementAction,
    ChannelInfo,
    ContactInfo,
    CampaignInfo,
    TenantInfo,
    EngagementMetadata,
    IdentifierType,
    Environment,
    GeoInfo,
    DeviceInfo,
    create_event_id,
)
from .outbox import (
    OutboxMessage,
    OutboxStatus,
)

__all__ = [
    # Events
    "UnifiedEngagementEvent",
    "ChannelType",
    "ProviderType",
    "EngagementAction",
    "ChannelInfo",
    "ContactInfo",
    "CampaignInfo",
    "TenantInfo",
    "EngagementMetadata",
    "IdentifierType",
    "Environment",
    "GeoInfo",
    "DeviceInfo",
    "create_event_id",
    # Outbox
    "OutboxMessage",
    "OutboxStatus",
]
