"""The webhook deliveries list ("Latest notices" in the panel) maps database rows to the model.

The response model requires `attempt_count`, but the listing built it with `retry_count` (a field
the model does not have), so GET /webhooks/{id}/events answered 500 as soon as a webhook had any
delivery. The router tests mock the whole listing, so only a row-level test catches it.
"""
from __future__ import annotations

import asyncio
import json
from datetime import datetime
from unittest.mock import patch

from mail_api.services.webhook_service import WebhookService


def _row(status: str, retry_count: int) -> dict:
    return {
        "id": 7, "webhook_id": 1, "event_id": "evt-1", "event_type": "delivered",
        "message_id": "4f6c2b3e", "recipient_email": "ana@example.org", "domain_name": "shop.example",
        "domain_id": 3, "payload": json.dumps({"event_type": "delivered"}), "status": status,
        "retry_count": retry_count, "response_code": 200 if status == "success" else None,
        "response_body": "ok", "error_message": None, "created_at": datetime(2026, 11, 9, 2, 0, 0),
        "processed_at": None, "sent_at": None, "next_retry_at": None,
    }


def _list(rows: list[dict]):
    async def fake_fetch_all(query, params):
        return rows

    with patch("mail_api.services.webhook_service.fetch_all_async", fake_fetch_all):
        return asyncio.run(WebhookService().list_webhook_events({"webhook_id": 1}))


def test_delivered_on_the_first_attempt():
    [event] = _list([_row("success", 0)])
    assert event.attempt_count == 1
    assert event.status == "success"


def test_retried_and_pending_events():
    retried, pending = _list([_row("retrying", 2), _row("pending", 0)])
    assert retried.attempt_count == 3
    assert pending.attempt_count == 0
