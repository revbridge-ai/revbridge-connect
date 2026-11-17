"""Tests for asynchronous bounce ingestion (`/bounces/async`).

The synchronous path (process_delivery_event) already suppresses hard bounces from
the Postfix log. This covers the gap: late DSNs that arrive as an email to the VERP
Return-Path `bounces+<rcpt>=<dom>@rbmXXXX...`. The handler decodes the VERP, reads
the DSN Status, and suppresses ONLY on hard bounces — reusing the existing
`apply_suppression_rules_for_bounce`.
"""
from __future__ import annotations

from unittest.mock import patch

import pytest
from mail_milter.events_updater.app import ingest_async_bounce
from mail_milter.events_updater.async_bounce import (
    decode_verp_recipient,
    extract_dsn_diagnostic,
    extract_dsn_status,
)
from starlette.requests import Request

_APP = "mail_milter.events_updater.app"


def _dsn(status: str | None, *, with_ds_part: bool = True, action: str = "failed") -> bytes:
    """Build a minimal RFC 3464 multipart/report DSN as raw bytes."""
    ds_part = ""
    if with_ds_part and status is not None:
        ds_part = (
            "--XYZ\r\n"
            "Content-Type: message/delivery-status\r\n\r\n"
            "Reporting-MTA: dns; mx.example.com\r\n\r\n"
            "Final-Recipient: rfc822; ana.silva@mail.example.org\r\n"
            f"Action: {action}\r\n"
            f"Status: {status}\r\n"
            "Diagnostic-Code: smtp; 550 user unknown\r\n"
        )
    return (
        "From: MAILER-DAEMON@mx.example.com\r\n"
        "To: bounces+ana.silva=mail.example.org@rbm100001.m.shop.example.com\r\n"
        "Subject: Undelivered Mail Returned to Sender\r\n"
        'Content-Type: multipart/report; report-type=delivery-status; boundary="XYZ"\r\n'
        "\r\n"
        "--XYZ\r\n"
        "Content-Type: text/plain\r\n\r\n"
        "Delivery to the following recipient failed permanently.\r\n\r\n"
        f"{ds_part}"
        "--XYZ--\r\n"
    ).encode()


def _make_request(body: bytes, headers: dict[str, str]) -> Request:
    scope = {
        "type": "http",
        "method": "POST",
        "headers": [(k.lower().encode(), v.encode()) for k, v in headers.items()],
    }
    delivered = {"done": False}

    async def receive() -> dict:
        if delivered["done"]:
            return {"type": "http.disconnect"}
        delivered["done"] = True
        return {"type": "http.request", "body": body, "more_body": False}

    return Request(scope, receive)


_VERP = "bounces+ana.silva=mail.example.org@rbm100001.m.shop.example.com"
_RCPT = "ana.silva@mail.example.org"
_RBM = "rbm100001.m.shop.example.com"


class TestDecodeVerpRecipient:
    @pytest.mark.parametrize(
        ("envelope", "expected"),
        [
            (_VERP, (_RCPT, _RBM)),
            (f"<{_VERP}>", (_RCPT, _RBM)),  # angle brackets stripped
            (
                "bounces+user+tag=example.com@rbm99.mail.shop.example",
                ("user+tag@example.com", "rbm99.mail.shop.example"),
            ),
            ("BOUNCES+A=EXAMPLE.COM@RBM1.m.x.com", ("a@example.com", "rbm1.m.x.com")),
        ],
    )
    def test_valid(self, envelope, expected):
        assert decode_verp_recipient(envelope) == expected

    @pytest.mark.parametrize(
        "envelope",
        [
            "",
            None,
            "ana@mail.example.org",                     # not a bounce address
            "bounces+a=example.com@mail.x.com",          # non-rbm domain
            "bounces+noequalshere@rbm1.m.x.com",         # missing '='
            "bounces+a=localpartonly@rbm1.m.x.com",      # rcpt domain has no dot
            "bounces+=example.com@rbm1.m.x.com",         # empty local
        ],
    )
    def test_invalid(self, envelope):
        assert decode_verp_recipient(envelope) is None


class TestExtractDsnStatus:
    @pytest.mark.parametrize(
        ("status", "expected"),
        [("5.1.1", "5.1.1"), ("4.2.2", "4.2.2"), ("5.7.1", "5.7.1")],
    )
    def test_reads_status(self, status, expected):
        assert extract_dsn_status(_dsn(status)) == expected

    def test_status_with_trailing_text(self):
        assert extract_dsn_status(_dsn("5.1.1 (bad mailbox)")) == "5.1.1"

    def test_missing_delivery_status_part(self):
        assert extract_dsn_status(_dsn(None, with_ds_part=False)) == ""

    def test_fallback_regex_on_plain_body(self):
        raw = b"From: x\r\nSubject: failed\r\n\r\nFinal-Recipient: a@b.com\r\nStatus: 5.4.4\r\n"
        assert extract_dsn_status(raw) == "5.4.4"

    def test_empty_body(self):
        assert extract_dsn_status(b"") == ""


class TestAsyncBounceEndpoint:
    async def test_hard_bounce_suppresses(self):
        req = _make_request(_dsn("5.1.1"), {"X-Bounce-Recipient": _VERP})
        with patch(f"{_APP}.bounce_matches_send", return_value=True), \
             patch(f"{_APP}.apply_suppression_rules_for_bounce") as m:
            m.return_value = {"suppressed": True}
            resp = await ingest_async_bounce(req)
        m.assert_called_once_with(_RCPT, _RBM, "hard")
        assert resp["processed"] is True
        assert resp["suppressed"] is True
        assert resp["bounce_type"] == "hard"

    async def test_hard_bounce_without_a_matching_send_is_ignored(self):
        # a forged DSN (no message of ours to that address) must not suppress anyone
        req = _make_request(_dsn("5.1.1"), {"X-Bounce-Recipient": _VERP})
        with patch(f"{_APP}.bounce_matches_send", return_value=False), \
             patch(f"{_APP}.apply_suppression_rules_for_bounce") as m:
            resp = await ingest_async_bounce(req)
        m.assert_not_called()
        assert resp["processed"] is False
        assert resp["reason"] == "unknown_message"

    @pytest.mark.parametrize("status", ["4.2.2", "5.7.1"])
    async def test_soft_and_policy_do_not_suppress(self, status):
        req = _make_request(_dsn(status), {"X-Bounce-Recipient": _VERP})
        with patch(f"{_APP}.apply_suppression_rules_for_bounce") as m:
            resp = await ingest_async_bounce(req)
        m.assert_not_called()
        assert resp["suppressed"] is False
        assert resp["bounce_type"] in {"soft", "policy"}

    async def test_invalid_verp_header_is_ignored(self):
        req = _make_request(_dsn("5.1.1"), {"X-Bounce-Recipient": "garbage@nope.com"})
        with patch(f"{_APP}.apply_suppression_rules_for_bounce") as m:
            resp = await ingest_async_bounce(req)
        m.assert_not_called()
        assert resp["processed"] is False
        assert resp["reason"] == "invalid_verp"

    async def test_no_dsn_status_does_not_suppress(self):
        req = _make_request(
            _dsn(None, with_ds_part=False), {"X-Bounce-Recipient": _VERP}
        )
        with patch(f"{_APP}.apply_suppression_rules_for_bounce") as m:
            resp = await ingest_async_bounce(req)
        m.assert_not_called()
        assert resp["suppressed"] is False
        assert resp["reason"] == "no_dsn_status"


class TestExtractDsnDiagnostic:
    def test_reads_diagnostic_code(self):
        assert extract_dsn_diagnostic(_dsn("5.1.1")) == "smtp; 550 user unknown"

    def test_folded_header_is_joined(self):
        raw = b"Status: 5.0.0\r\nDiagnostic-Code: smtp; 550 Email blocked\r\n by dnsbl.spfbl.net\r\n"
        assert extract_dsn_diagnostic(raw) == "smtp; 550 Email blocked by dnsbl.spfbl.net"

    def test_ignores_original_message_body(self):
        # the DSN carries the original content; "blocklist" in the body is NOT a diagnostic
        raw = _dsn("5.1.1") + b"\r\nCheck out our blocklist of offers!\r\n"
        assert "blocklist" not in extract_dsn_diagnostic(raw)

    def test_empty(self):
        assert extract_dsn_diagnostic(b"") == ""
