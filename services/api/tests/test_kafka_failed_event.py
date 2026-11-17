"""
'failed' event before Postfix (email_processor → kafka_outbox).

A permanent failure in the worker never goes through the milter (the normal
delivery audit), so the failed event comes from the processor itself. These
tests cover the kafka_client layer: the FAILED action, passing on the
campaign_id (the client's X-Connect-Campaign header) and the error fields in
the metadata.
"""

from unittest.mock import patch

from mail_api.common.kafka_client import (
    KafkaClient,
    KafkaEventType,
    _create_email_unified_event,
)
from connect_lib.messaging import EngagementAction


def _build(payload):
    return _create_email_unified_event(
        event_type=KafkaEventType.FAILED,
        message_id="msg-123",
        recipient_email="dest@example.com",
        domain_name="example.com",
        payload_data=payload,
    )


def test_failed_event_action_and_error_fields():
    event = _build({
        "error_message": "No email_config found for ana@example.org",
        "error_code": "501",
        "campaign_id": None,
    })
    assert event.action == EngagementAction.FAILED
    assert event.metadata.error_code == "501"
    assert "email_config" in event.metadata.error_message
    # No campaign_id → campaign=None
    assert event.campaign is None


def test_failed_event_campaign_attribution():
    event = _build({
        "error_message": "boom",
        "error_code": None,
        "campaign_id": "camp-1",
    })
    assert event.campaign is not None
    assert event.campaign.id == "camp-1"
    assert event.campaign.message_id == "msg-123"


def test_queue_email_failed_inserts_outbox():
    client = KafkaClient()
    client.enabled = True
    with patch.object(client, "_insert_into_outbox", return_value=42) as ins:
        ok = client.queue_email_failed(
            message_id="msg-123",
            recipient_email="dest@example.com",
            domain_name="example.com",
            error_message="No email_config found",
            error_code="550",
            campaign_id="camp-1",
        )
    assert ok is True
    kwargs = ins.call_args.kwargs
    assert kwargs["event_type"] == "failed"
    assert kwargs["rcpt"] == "dest@example.com"
    event = kwargs["event"]
    assert event.action == EngagementAction.FAILED
    assert event.campaign.id == "camp-1"


def test_tracking_events_keep_no_campaign():
    # opened/clicked carry no CampaignInfo (their attribution comes from the
    # tracking token, not from this path).
    event = _create_email_unified_event(
        event_type=KafkaEventType.OPENED,
        message_id="msg-1",
        recipient_email="a@b.c",
        domain_name="b.c",
        payload_data={"user_agent": "UA"},
    )
    assert event.campaign is None
    assert event.action == EngagementAction.OPENED
