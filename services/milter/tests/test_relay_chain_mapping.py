"""Mapping chain on the hop from the local Postfix to an internal smarthost.

The anti-double-count guard drops the internal hop (relay ending in
CONNECT_INTERNAL_RELAY_SUFFIX), but that hop is the event that carries the
`q:{queue_id}:{rcpt}` mapping written by the milter. The REAL delivery, logged by
a smarthost without a milter, arrives with a queue_id that was never mapped; it
would get a synthetic `smtp-delivered-*` message_id, with no campaign attached,
and the tracking service's "no delivered" gate would hold back the opens.

Before dropping the hop, `chain_relay_queue_mapping` extracts the
"queued as <QID>" from the line and propagates the mapping to `q:{QID}:{rcpt}`
(SET NX + TTL), so the smarthost's delivery resolves the real message_id.
"""
from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

_APP = "mail_milter.events_updater.app"

_HOP_LINE = (
    "2026-07-21T13:00:07+00:00 mta-0 postfix/smtp[123]: 5846560013: "
    "to=<ana.silva12@example.com>, "
    "relay=pool-a.mta.example.com[203.0.113.119]:25, "
    "delay=1.2, delays=0.3/0.1/0.2/0.6, dsn=2.0.0, "
    "status=sent (250 2.0.0 Ok: queued as 53BE460191)"
)

_MAPPING = {
    "message_id": "926c43d1-aca8-46a6-a96d-c3cbfa6ed8f6",
    "rcpt": "ana.silva12@example.com",
    "domain_name": "rbm178765.mail.shop.example.com",
    "queue_id": "5846560013",
    "trace_id": "t1",
}


def test_chain_writes_downstream_mapping_with_nx():
    """Hop with 'queued as' + a source mapping -> writes q:{smarthost_qid}:{rcpt}."""
    redis_mock = MagicMock()
    redis_mock.get.return_value = json.dumps(_MAPPING)
    with patch(f"{_APP}.redis", redis_mock):
        from mail_milter.events_updater.app import chain_relay_queue_mapping

        result = chain_relay_queue_mapping(
            "5846560013", "ana.silva12@example.com", _HOP_LINE
        )

    assert result == "53BE460191"
    redis_mock.set_if_absent.assert_called_once()
    key, ttl, value = redis_mock.set_if_absent.call_args[0]
    assert key == "q:53BE460191:ana.silva12@example.com"
    assert ttl > 0
    chained = json.loads(value)
    assert chained["message_id"] == _MAPPING["message_id"]
    assert chained["queue_id"] == "53BE460191"
    assert chained["chained_from"] == "5846560013"
    assert chained["domain_name"] == _MAPPING["domain_name"]


def test_chain_uses_prefetch_cache_without_redis_get():
    """Mapping already in the batch cache -> no extra Redis GET."""
    redis_mock = MagicMock()
    cache = {"q:5846560013:ana.silva12@example.com": dict(_MAPPING)}
    with patch(f"{_APP}.redis", redis_mock):
        from mail_milter.events_updater.app import chain_relay_queue_mapping

        result = chain_relay_queue_mapping(
            "5846560013", "ana.silva12@example.com", _HOP_LINE, cache=cache
        )

    assert result == "53BE460191"
    redis_mock.get.assert_not_called()
    redis_mock.set_if_absent.assert_called_once()


def test_chain_noop_without_queued_as():
    """A line without 'queued as' (e.g. a kept external relay) -> writes nothing."""
    redis_mock = MagicMock()
    line = _HOP_LINE.replace(" Ok: queued as 53BE460191", " Ok")
    with patch(f"{_APP}.redis", redis_mock):
        from mail_milter.events_updater.app import chain_relay_queue_mapping

        result = chain_relay_queue_mapping(
            "5846560013", "ana.silva12@example.com", line
        )

    assert result is None
    redis_mock.set_if_absent.assert_not_called()


def test_chain_noop_without_source_mapping():
    """No source mapping (TTL expired / the milter never saw it) -> writes nothing."""
    redis_mock = MagicMock()
    redis_mock.get.return_value = None
    with patch(f"{_APP}.redis", redis_mock):
        from mail_milter.events_updater.app import chain_relay_queue_mapping

        result = chain_relay_queue_mapping(
            "5846560013", "ana.silva12@example.com", _HOP_LINE
        )

    assert result is None
    redis_mock.set_if_absent.assert_not_called()


def test_chain_supports_long_queue_ids():
    """Postfix long queue IDs are mixed-case alphanumeric."""
    redis_mock = MagicMock()
    redis_mock.get.return_value = json.dumps(_MAPPING)
    line = _HOP_LINE.replace("queued as 53BE460191", "queued as 4Wxq3D1c1Vz9rWs")
    with patch(f"{_APP}.redis", redis_mock):
        from mail_milter.events_updater.app import chain_relay_queue_mapping

        result = chain_relay_queue_mapping(
            "5846560013", "ana.silva12@example.com", line
        )

    assert result == "4Wxq3D1c1Vz9rWs"


def test_chain_fail_open_on_redis_error():
    """A Redis error must not take down the batch processing."""
    redis_mock = MagicMock()
    redis_mock.get.side_effect = RuntimeError("redis down")
    with patch(f"{_APP}.redis", redis_mock):
        from mail_milter.events_updater.app import chain_relay_queue_mapping

        result = chain_relay_queue_mapping(
            "5846560013", "ana.silva12@example.com", _HOP_LINE
        )

    assert result is None


def test_extract_from_message_short_queue_id():
    """Short queue IDs (5-9 hex, e.g. '7176A17') must be extracted too;
    otherwise the event would get a synthetic message_id."""
    from mail_milter.events_updater.app import extract_from_message

    line = (
        "2026-07-21T21:08:02+00:00 mta-2 postfix/smtp/smtp[372]: "
        "7176A17: to=<bruno@example.com>, "
        "relay=pool-01.mta.example.com[203.0.113.172]:25, "
        "delay=0.89, delays=0.76/0.02/0.07/0.04, dsn=2.0.0, "
        "status=sent (250 2.0.0 Ok: queued as 505A8609BB)"
    )
    qid, rcpt = extract_from_message(line)
    assert qid == "7176A17"
    assert rcpt == "bruno@example.com"


def test_extract_from_message_long_queue_id_still_works():
    from mail_milter.events_updater.app import extract_from_message

    line = (
        "2026-07-21T13:00:07+00:00 host postfix/smtp[1]: 53BE460191: "
        "to=<a@b.com>, relay=gmail-smtp-in.l.google.com[192.0.2.4]:25, "
        "delay=1, delays=0.3/0.1/0.2/0.6, dsn=2.0.0, status=sent (250 ok)"
    )
    qid, rcpt = extract_from_message(line)
    assert qid == "53BE460191"
    assert rcpt == "a@b.com"
