"""Delivery events carry the customer's campaign_id (X-Connect-Campaign header) when there is one.

The milter/events-updater kafka_client puts the payload's campaign_id into the CampaignInfo of
the event written to the outbox; without a campaign_id, the event has no CampaignInfo.
"""
from __future__ import annotations

from mail_milter.common.kafka_client import KafkaEventType, _create_unified_event


def test_delivered_event_carries_campaign_id():
    evt = _create_unified_event(
        KafkaEventType.DELIVERED, "msg-1", "x@y.com", "d",
        {"campaign_id": "camp-1"},
    )
    assert evt.campaign is not None
    assert evt.campaign.id == "camp-1"
    assert evt.campaign.message_id == "msg-1"


def test_no_campaign_means_no_campaign_info():
    evt = _create_unified_event(
        KafkaEventType.DELIVERED, "m", "x@y.com", "d", {},
    )
    assert evt.campaign is None
