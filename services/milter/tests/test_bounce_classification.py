"""Tests for DSN-based bounce classification and the suppression gate.

A 5.7.x *policy* bounce (e.g. a relay's `550-5.7.1 Daily SMTP relay limit
exceeded`) is not an invalid recipient, so the events-updater must:

  - classify 5.7.x as ``policy`` (not ``hard``);
  - only suppress on real ``hard`` bounces.

5.5.x (e.g. DSN 5.5.2 "syntax error") is an SMTP protocol error on the sending
path, never the recipient's fault, and must not suppress either.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from mail_milter.events_updater.app import classify_bounce_type

_APP = "mail_milter.events_updater.app"


class TestClassifyBounceType:
    @pytest.mark.parametrize(
        ("dsn", "expected"),
        [
            ("4.2.2", "soft"),
            ("4.7.0", "soft"),
            ("5.7.1", "policy"),   # relay daily limit
            ("5.7.26", "policy"),
            ("5.5.2", "protocol"),  # syntax error on the sending path
            ("5.5.0", "protocol"),
            ("5.5.4", "protocol"),
            ("5.1.1", "hard"),     # user unknown
            ("5.4.4", "hard"),
            ("5.1.7", "hard"),     # malformed address
            ("", "hard"),          # no DSN -> conservative legacy default
            (None, "hard"),
        ],
    )
    def test_classification(self, dsn, expected):
        assert classify_bounce_type(dsn) == expected


_SPFBL_MSG = (
    "823516078D: to=<contact@store.example.org>, relay=mail.store.example.org"
    "[198.51.100.89]:25, delay=23, dsn=5.0.0, status=bounced (host mail.store.example"
    ".org[198.51.100.89] said: 550 Email blocked by dnsbl.spfbl.net (127.0.0.3) "
    "(in reply to RCPT TO command))"
)


class TestBlocklistRejection:
    """An MX that answers `550` without an enhanced code makes Postfix log dsn=5.0.0.
    A blocklist rejection of the sending IP must not become a hard bounce (it would
    suppress a VALID address)."""

    @pytest.mark.parametrize(
        "msg",
        [
            _SPFBL_MSG,
            "said: 554 5.7.1 Service unavailable; Client host [192.0.2.4] blocked using zen.spamhaus.org",
            "said: 550 Your IP is on our blacklist",
            "said: 550 rejected because 192.0.2.4 is in a black list at b.barracudacentral.org",
            "said: 550 Sender IP has poor reputation",
            "said: 554 Rejected - listed on RBL",
        ],
    )
    def test_blocklist_text_is_policy(self, msg):
        assert classify_bounce_type("5.0.0", msg) == "policy"
        assert classify_bounce_type("", msg) == "policy"

    @pytest.mark.parametrize(
        "msg",
        [
            "said: 550 5.1.1 User unknown",
            # 'listed' alone is NOT reputation: this is a genuinely nonexistent recipient
            "said: 550 5.4.1 Recipient address rejected: User not listed in public Exchange directory",
            "said: 550 Mailbox unavailable",
        ],
    )
    def test_invalid_recipient_text_stays_hard(self, msg):
        assert classify_bounce_type("5.0.0", msg) == "hard"

    @pytest.mark.parametrize(
        "msg",
        [
            # MX hosted on Barracuda: the HOST NAME is not a rejection reason
            "to=<ana@example.com>, relay=d123.ess.barracudanetworks.com[192.0.2.4]:25, "
            "dsn=5.0.0, status=bounced (host d123.ess.barracudanetworks.com[192.0.2.4] said: "
            "550 permanent failure for one or more recipients (ana@example.com:550 5.4.1 "
            "Recipient address rejected: Access denied.) (in reply to end of DATA command))",
            # generic cPanel/Exim text: covers a nonexistent address
            "said: 550-The mail server could not deliver mail to info@example.org.  The account "
            "550-or domain may not exist, they may be blacklisted, or missing the proper dns",
            # the recipient itself blocked the sender: suppressing is right
            "said: 550 5.5.1 Your_email_is_rejected_--_you_are_on_the_recipient's_blacklist.",
        ],
    )
    def test_ambiguous_or_recipient_side_stays_hard(self, msg):
        assert classify_bounce_type("5.0.0", msg) == "hard"

    @pytest.mark.parametrize(
        "msg",
        [
            'said: 550-"JunkMail rejected - pool-02.mta.example.com 550-[203.0.113.119]'
            ':34492 is in an RBL: 550 https://matrix.spfbl.net/203.0.113.119"',
            "said: 553 Message rejected due to IP reputation. Your IP (198.51.100.164) is listed "
            "on our internal blocklist.",
            "said: 552 5.2.0 Remote MTA 198.51.100.164: An URL contained in this message is "
            "blacklisted by Abusix Domain Blacklist.",
        ],
    )
    def test_real_ip_or_content_blocks_are_policy(self, msg):
        assert classify_bounce_type("5.0.0", msg) == "policy"

    def test_soft_stays_soft_even_with_blocklist_text(self):
        assert classify_bounce_type("4.7.1", _SPFBL_MSG) == "soft"


def _drive_bounce(dsn: str, status_msg: str = ""):
    """Run process_delivery_event for a bounce with deps stubbed (mirrors the
    harness in test_outbox_dedup_gate). Returns the
    apply_suppression_rules_for_bounce mock so the test can assert whether
    suppression was attempted.
    """
    redis_mock = MagicMock()
    redis_mock.get.return_value = None
    redis_mock.scan_keys.return_value = []

    with patch(f"{_APP}.fetch_one", return_value=None), \
         patch(f"{_APP}.fetch_all", return_value=[]), \
         patch(f"{_APP}.exec_sql", return_value=None), \
         patch(f"{_APP}.upsert_delivery_returning_changed", return_value=True), \
         patch(f"{_APP}.get_domain_id", return_value=1), \
         patch(f"{_APP}.map_delivery_status", return_value="bounced"), \
         patch(f"{_APP}.redis", redis_mock), \
         patch(f"{_APP}.trigger_kafka_event"), \
         patch(f"{_APP}.apply_suppression_rules_for_bounce") as suppress:
        from mail_milter.events_updater.app import process_delivery_event

        process_delivery_event(
            message_id="<rbm-test@mail.example.com>",
            rcpt="user@example.com",
            queue_id="ABC123",
            status="bounced",
            dsn=dsn,
            host="mx.example.com",
            service="smtp",
            timestamp="2026-06-12T12:00:00Z",
            status_msg=status_msg,
        )
    return suppress


class TestSuppressionGate:
    def test_policy_bounce_does_not_suppress(self):
        suppress = _drive_bounce("5.7.1")
        suppress.assert_not_called()

    def test_protocol_bounce_does_not_suppress(self):
        suppress = _drive_bounce("5.5.2")
        suppress.assert_not_called()

    def test_soft_bounce_does_not_suppress(self):
        suppress = _drive_bounce("4.2.2")
        suppress.assert_not_called()

    def test_hard_bounce_suppresses(self):
        suppress = _drive_bounce("5.1.1")
        assert suppress.called
        # bounce_type is recorded on the suppression row
        _, kwargs = suppress.call_args
        assert kwargs.get("bounce_type") == "hard"

    def test_blocklist_rejection_does_not_suppress(self):
        suppress = _drive_bounce("5.0.0", _SPFBL_MSG)
        suppress.assert_not_called()
