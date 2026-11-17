"""Tests for the pure rules of the webhook dispatcher (RevBridge Connect)."""

import hashlib
import hmac
import json

from mail_api.webhook_dispatch import dispatcher as d
from mail_api.webhook_dispatch import ssrf
from mail_api.webhook_dispatch.payloads import WebhookEventType, WebhookQueueMessage, create_event_payload, event_type_variants


def test_in_family_only_own_domain_and_subdomains():
    assert d.in_family("shop.example", "shop.example")
    assert d.in_family("rbm123.shop.example", "shop.example")
    assert d.in_family("RBM123.Shop.EXAMPLE.", "shop.example")
    assert not d.in_family("othershop.example", "shop.example")
    assert not d.in_family("shop.example.evil.com", "shop.example")
    assert not d.in_family("shop.example", None)
    assert not d.in_family(None, "shop.example")


def test_unwrap_engagement_click_becomes_clicked():
    ev = d.unwrap_event({
        "event_type": "engagement", "action": "click",
        "channel": {"type": "email", "provider_event_id": "m-1"},
        "contact": {"identifier": "a@example.org", "identifier_type": "email"},
        "metadata": {"domain_name": "rbm1.shop.example", "click_url": "https://example.org/o"},
        "tenant": {"id": "rbm1.shop.example"},
    })
    assert ev["event_type"] == "clicked" and ev["message_id"] == "m-1" and ev["recipient"] == "a@example.org"
    assert ev["domain_name"] == "rbm1.shop.example" and ev["payload_data"]["click_url"] == "https://example.org/o"


def test_unwrap_generic_redis_event():
    ev = d.unwrap_event({"event_type": "complaint", "message_id": "m-2", "recipient_email": "b@example.org",
                         "domain_name": "shop.example", "payload_data": {"complaint_type": "abuse"}})
    assert ev["event_type"] == "complaint" and ev["recipient"] == "b@example.org" and ev["payload_data"]["complaint_type"] == "abuse"


def test_lift_specific_fields():
    assert d.lift_specific_fields("clicked", {"click_url": "https://example.org"})["clicked_url"] == "https://example.org"
    out = d.lift_specific_fields("delivered", {"provider_data": {"delivery_time": "2026-11-08T01:00:00Z", "server_response": "250 ok"}})
    assert out["delivery_time"] == "2026-11-08T01:00:00Z" and out["server_response"] == "250 ok"
    out = d.lift_specific_fields("hard_bounced", {"error_code": 550, "error_message": "user unknown"})
    assert out["bounce_code"] == "550" and out["bounce_reason"] == "user unknown"


def test_click_aliases_match_both_spellings():
    assert set(event_type_variants(WebhookEventType.CLICKED)) == {"click", "clicked"}


def test_bounced_subscription_receives_hard_and_soft_bounces():
    # the panel offers only "bounced"; the pipeline emits hard_bounced / soft_bounced
    assert "bounced" in event_type_variants(WebhookEventType.HARD_BOUNCED)
    assert "bounced" in event_type_variants(WebhookEventType.SOFT_BOUNCED)
    assert event_type_variants(WebhookEventType.BOUNCED) == ["bounced"]


def test_bounce_payload_without_type_gets_hard():
    p = create_event_payload(event_type=WebhookEventType.BOUNCED, message_id="m", domain_id=1)
    assert p.bounce_type == "hard"


def test_retry_delay_grows_and_caps(monkeypatch):
    monkeypatch.setattr(d, "RETRY_DELAY", 5)
    monkeypatch.setattr(d, "MAX_RETRY_DELAY", 60)
    assert [d.retry_delay(n) for n in (1, 2, 3, 4, 5)] == [5, 10, 20, 40, 60]


def test_request_headers_signature_and_extras():
    msg = WebhookQueueMessage(webhook_id=1, event_type=WebhookEventType.DELIVERED, message_id="m-9", payload={"a": 1})
    body = json.dumps(msg.payload)
    h = d.request_headers(msg, {"secret_key": "s3cr3t", "headers": json.dumps({"X-Customer": "1"})}, body)
    assert h["X-Webhook-Signature"] == "sha256=" + hmac.new(b"s3cr3t", body.encode(), hashlib.sha256).hexdigest()
    assert h["User-Agent"] == "RevBridge-Webhook/1.0" and h["X-Webhook-Event"] == "delivered"
    assert h["X-Webhook-Message-Id"] == "m-9" and h["X-Customer"] == "1"
    assert "X-Webhook-Signature" not in d.request_headers(msg, {}, body)


def test_ssrf_blocks_internal_and_bad_schemes(monkeypatch):
    monkeypatch.delenv("WEBHOOK_ALLOW_PRIVATE_HOSTS", raising=False)
    for url in ("http://127.0.0.1/x", "http://10.0.0.5/", "http://169.254.169.254/latest", "http://[::1]/",
                "ftp://example.org/", "http://user:pw@example.org/", "http:///nohost"):
        try:
            ssrf.check_url(url)
        except ssrf.BlockedDestination:
            continue
        raise AssertionError(f"should block {url}")


def test_ssrf_allowlist_is_explicit(monkeypatch):
    monkeypatch.setenv("WEBHOOK_ALLOW_PRIVATE_HOSTS", "localhost")
    ssrf.check_url("http://localhost:8099/a")  # allowed by name only
    try:
        ssrf.check_url("http://127.0.0.1:8099/a")
    except ssrf.BlockedDestination:
        return
    raise AssertionError("an internal IP outside the list must stay blocked")
