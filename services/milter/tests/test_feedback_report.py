"""Tests for complaint report parsing (ARF, RFC 5965)."""

from mail_milter.events_updater.feedback_report import parse_feedback_report

ARF = b"""From: feedback@fbl.example.net
To: bounces+fbl@rbm123.shop.example.com
Subject: FW: offer
MIME-Version: 1.0
Content-Type: multipart/report; report-type=feedback-report; boundary="B"

--B
Content-Type: text/plain

abuse report

--B
Content-Type: message/feedback-report

Feedback-Type: abuse
User-Agent: ExampleFBL/1.0
Version: 1
Original-Mail-From: <bounces+maria=example.net@rbm123.shop.example.com>
Original-Rcpt-To: <Maria@Example.net>
Arrival-Date: Sat, 08 Nov 2026 01:00:00 +0000
Source-IP: 203.0.113.25
Reported-Domain: shop.example.com

--B
Content-Type: text/rfc822-headers

Return-Path: <bounces+maria=example.net@rbm123.shop.example.com>
Message-ID: <6ecb1a0e-1b2c-4d5e-8f90-112233445566@shop.example.com>
X-Connect-ID: 6ecb1a0e-1b2c-4d5e-8f90-112233445566
From: Shop <contact@shop.example.com>
To: maria@example.net
Subject: offer

--B--
"""


def test_parses_arf_fields():
    r = parse_feedback_report(ARF)
    assert r["feedback_type"] == "abuse" and r["user_agent"] == "ExampleFBL/1.0"
    assert r["source_ip"] == "203.0.113.25" and r["reported_domain"] == "shop.example.com"
    assert r["rcpt"] == "maria@example.net"
    assert r["message_ids"][0] == "6ecb1a0e-1b2c-4d5e-8f90-112233445566"
    assert r["verp_domain"] == "rbm123.shop.example.com" and r["from_domain"] == "shop.example.com"


def test_rcpt_falls_back_to_original_to():
    raw = ARF.replace(b"Original-Rcpt-To: <Maria@Example.net>\n", b"")
    assert parse_feedback_report(raw)["rcpt"] == "maria@example.net"


def test_invalid_source_ip_is_dropped():
    raw = ARF.replace(b"Source-IP: 203.0.113.25", b"Source-IP: not-an-ip")
    assert parse_feedback_report(raw)["source_ip"] is None


def test_dsn_is_not_a_feedback_report():
    dsn = ARF.replace(b"report-type=feedback-report", b"report-type=delivery-status")
    assert parse_feedback_report(dsn) is None
    assert parse_feedback_report(b"From: a@b\nSubject: hi\n\nbody") is None
