"""The MIME message goes out with Sender = From: some relays inject their own
Sender (<local>=<domain>@relay.example → "on behalf of" in Outlook) only when it
is missing.
"""

from mail_api.services.email_service import MailService


def _mail(headers: dict | None = None) -> dict:
    return {
        "headers": headers or {},
        "from": {"address": "contact@updates.shop.example", "name": "Shop"},
        "to": [{"address": "ana@example.org", "name": "Ana"}],
        "subject": "Hi",
        "content": [{"type": "text/plain", "value": "ok"}],
    }


def _create(mail: dict):
    return MailService.__new__(MailService)._create_message(mail)


def test_sender_equals_from():
    msg = _create(_mail())
    assert msg["Sender"] == "Shop <contact@updates.shop.example>"
    assert msg["Sender"] == msg["From"]
    assert len(msg.get_all("Sender")) == 1


def test_sender_from_custom_headers_is_kept():
    msg = _create(_mail({"sender": "Other <x@updates.shop.example>"}))
    assert msg.get_all("Sender") == ["Other <x@updates.shop.example>"]


def test_date_header_is_set_and_a_custom_one_is_kept():
    from email.utils import parsedate_to_datetime

    msg = _create(_mail())
    assert len(msg.get_all("Date")) == 1
    assert parsedate_to_datetime(msg["Date"]).tzinfo is not None
    custom = _create(_mail({"Date": "Thu, 12 Nov 2026 10:00:00 +0000"}))
    assert custom.get_all("Date") == ["Thu, 12 Nov 2026 10:00:00 +0000"]
