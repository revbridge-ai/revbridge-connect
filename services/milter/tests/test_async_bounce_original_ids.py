"""extract_original_message_ids: ids of the ORIGINAL message quoted in a DSN (pure function)."""

from mail_milter.events_updater.async_bounce import extract_original_message_ids

UUID = "4f6c2b3e-1a2b-4c3d-8e9f-0123456789ab"

DSN_MIME = (
    "From: MAILER-DAEMON@mx.example.org\r\n"
    "To: bounces+ana=example.org@rbm1.shop.example\r\n"
    "Message-ID: <dsn-0001@mx.example.org>\r\n"
    "MIME-Version: 1.0\r\n"
    'Content-Type: multipart/report; report-type=delivery-status; boundary="b"\r\n'
    "\r\n"
    "--b\r\nContent-Type: text/plain\r\n\r\nDelivery failed.\r\n"
    "--b\r\nContent-Type: message/delivery-status\r\n\r\n"
    "Reporting-MTA: dns; mx.example.org\r\n\r\n"
    "Final-Recipient: rfc822; ana@example.org\r\nAction: failed\r\nStatus: 5.1.1\r\n"
    "--b\r\nContent-Type: text/rfc822-headers\r\n\r\n"
    f"Message-ID: <{UUID}@mail.example.com>\r\n"
    f"X-Connect-ID: {UUID}\r\n"
    "Subject: Hello\r\n"
    "--b--\r\n"
).encode()


def test_ids_come_from_the_original_headers_not_from_the_dsn():
    ids = extract_original_message_ids(DSN_MIME)
    assert ids[0] == UUID
    assert "dsn-0001" not in ids


def test_plain_dsn_without_mime_parts_uses_the_quoted_headers_only():
    raw = (
        "From: MAILER-DAEMON@mx.example.org\n"
        "Message-ID: <dsn-0002@mx.example.org>\n"
        "\n"
        "Your message could not be delivered.\n"
        f"Message-ID: <{UUID}@mail.example.com>\n"
    ).encode()
    ids = extract_original_message_ids(raw)
    assert UUID in ids
    assert "dsn-0002" not in ids


def test_dsn_without_original_headers_returns_nothing():
    raw = b"From: MAILER-DAEMON@mx.example.org\nMessage-ID: <dsn-3@mx.example.org>\n\nGone.\n"
    assert extract_original_message_ids(raw) == []
