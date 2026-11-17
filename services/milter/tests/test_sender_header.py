"""The milter adds Sender = From when it is missing: otherwise a relay that
rewrites the envelope may add its own "Sender: <local>=<domain>@relay.example.net"
and Outlook shows "on behalf of". Covers direct SMTP, which does not go through
the API's /send.
"""
import email.utils as _email_utils
import sys
import types
from unittest.mock import MagicMock

# pymilter only exists in the milter image: minimal stub before importing
# milter_server (same pattern as test_milter_per_message_reset.py).
if "Milter" not in sys.modules:
    _stub = types.ModuleType("Milter")

    class _Base:
        def __init__(self, *a, **k):
            pass

    _stub.Milter = _Base
    _stub.CONTINUE = 0
    _stub.REJECT = 1
    _stub.TEMPFAIL = 2
    _stub.ACCEPT = 3
    _stub.DISCARD = 4
    _stub.ADDRCPT = 8
    _stub.CHGHDRS = 16
    _stub.CHGFROM = 32
    _stub.factory = None
    _stub.runmilter = lambda *a, **k: None
    _utils = types.ModuleType("Milter.utils")
    _utils.parseaddr = _email_utils.parseaddr
    _stub.utils = _utils
    sys.modules["Milter"] = _stub
    sys.modules["Milter.utils"] = _utils

from mail_milter.tracking_milter.milter_server import TrackingMilter


def _milter(headers: dict) -> TrackingMilter:
    m = TrackingMilter.__new__(TrackingMilter)
    m.headers = headers
    m.addheader = MagicMock()
    return m


def test_adds_a_sender_equal_to_from():
    m = _milter({"from": ['"corporate@travel.example.org" <no-reply@m.shop.example.com>']})
    m._ensure_sender_header()
    m.addheader.assert_called_once_with(
        "Sender", '"corporate@travel.example.org" <no-reply@m.shop.example.com>'
    )
    assert m.headers["sender"] == ['"corporate@travel.example.org" <no-reply@m.shop.example.com>']


def test_folded_from_becomes_one_line():
    m = _milter({"from": ["Shop\r\n\t<contact@mail.shop.example.com>"]})
    m._ensure_sender_header()
    m.addheader.assert_called_once_with("Sender", "Shop <contact@mail.shop.example.com>")


def test_existing_sender_is_kept():
    # The API already sends Sender = From; it must not be duplicated.
    m = _milter({"from": ["Shop <a@shop.example>"], "sender": ["Shop <a@shop.example>"]})
    m._ensure_sender_header()
    m.addheader.assert_not_called()


def test_no_from_adds_nothing():
    m = _milter({"subject": ["x"]})
    m._ensure_sender_header()
    m.addheader.assert_not_called()


def test_feedback_id_uses_the_campaign_and_the_from_domain():
    m = _milter({"from": ['Shop <hello@onboarding.shop.example>']})
    m.campaign_id = "Black Friday/2026"
    m._ensure_feedback_id()
    m.addheader.assert_called_once_with("Feedback-ID", "Black-Friday-2026:mail:connect:onboarding.shop.example")
    assert m.headers["feedback-id"] == ["Black-Friday-2026:mail:connect:onboarding.shop.example"]


def test_feedback_id_without_a_campaign_and_existing_one_is_kept():
    m = _milter({"from": ["hello@shop.example"]})
    m.campaign_id = None
    m._ensure_feedback_id()
    m.addheader.assert_called_once_with("Feedback-ID", "none:mail:connect:shop.example")
    kept = _milter({"from": ["hello@shop.example"], "feedback-id": ["x:y:z:w"]})
    kept.campaign_id = None
    kept._ensure_feedback_id()
    kept.addheader.assert_not_called()


def test_feedback_id_needs_a_from_domain():
    m = _milter({"subject": ["x"]})
    m.campaign_id = None
    m._ensure_feedback_id()
    m.addheader.assert_not_called()
