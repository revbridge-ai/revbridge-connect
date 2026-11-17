"""The events_updater hot path must NOT scan Redis (SCAN).

When a message keeps being deferred, its retries can outlive the primary key
`q:{queue_id}:{rcpt}` (TTL expired). A keyspace scan on every such miss
(`q:{queue_id}:*`, or worse `q:*:{rcpt}`) would cost seconds per event on a large
Redis and saturate the ingest workers. On a miss the lookup is O(1): only the GET
of the primary key, and domain_name comes from the rcpt itself. These tests pin
that NO scan_keys call runs on the hot path.
"""
from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

_APP = "mail_milter.events_updater.app"


def test_find_message_id_no_scan_on_miss():
    """Miss on the primary key -> returns None WITHOUT calling scan_keys."""
    redis_mock = MagicMock()
    redis_mock.get.return_value = None  # primary key miss
    with patch(f"{_APP}.redis", redis_mock):
        from mail_milter.events_updater.app import find_message_id_by_queue

        result = find_message_id_by_queue("QID123", "user@example.com")

    assert result is None
    redis_mock.scan_keys.assert_not_called()  # <- the main point


def test_find_message_id_hit_returns_msgid_no_scan():
    """Hit on the primary key -> returns the message_id, also without scan_keys."""
    redis_mock = MagicMock()
    redis_mock.get.return_value = json.dumps(
        {"message_id": "<m1@mail.example.com>", "trace_id": "t1"}
    )
    with patch(f"{_APP}.redis", redis_mock):
        from mail_milter.events_updater.app import find_message_id_by_queue

        result = find_message_id_by_queue("QID123", "user@example.com")

    assert result == "<m1@mail.example.com>"
    redis_mock.scan_keys.assert_not_called()


def test_process_delivery_event_domain_lookup_no_scan():
    """The domain lookup inside process_delivery_event does not scan Redis on a
    miss; domain_name comes from the rcpt itself (O(1) fallback)."""
    redis_mock = MagicMock()
    redis_mock.get.return_value = None  # every Redis lookup misses
    redis_mock.scan_keys.return_value = []

    def _fetch_one(sql, params=None):
        return None

    with patch(f"{_APP}.fetch_one", side_effect=_fetch_one), \
         patch(f"{_APP}.fetch_all", return_value=[]), \
         patch(f"{_APP}.exec_sql", return_value=None), \
         patch(f"{_APP}.upsert_delivery_returning_changed", return_value=True), \
         patch(f"{_APP}.get_domain_id", return_value=1), \
         patch(f"{_APP}.map_delivery_status", return_value="delivered"), \
         patch(f"{_APP}.redis", redis_mock), \
         patch(f"{_APP}.trigger_kafka_event"):
        from mail_milter.events_updater.app import process_delivery_event

        result = process_delivery_event(
            message_id="<msg-1@mail.example.com>",
            rcpt="user@gmail.com",
            queue_id="ABC123",
            status="sent",
            dsn="2.0.0",
            host="mx.example.com",
            service="smtp",
            timestamp="2026-06-26T12:00:00Z",
        )

    # No SCAN anywhere while processing the event
    redis_mock.scan_keys.assert_not_called()
    # And domain_name was resolved from the rcpt (gmail.com), not "unknown"
    assert result["status"] == "success"
