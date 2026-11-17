"""A Postfix bounce is internal only when the hop that refused it is one of our own nodes.

Every log line also carries the syslog host of the node that wrote it, and in RevBridge Connect
that is always one of the installation's own nodes (mta1.<install domain>). Checking that host
against CONNECT_INTERNAL_RELAY_SUFFIX dropped every synchronous bounce as "internal": the email
stayed "sent", no `bounced` webhook went out and the health brakes never saw bounces or blocks.
The check now uses the `relay=` host, like the delivered path.
"""
from __future__ import annotations

from contextlib import contextmanager

import mail_milter.events_updater.app as app

SUFFIX = ".mail.example.com"

REMOTE_REFUSAL = (
    "2026-11-09T02:14:19+00:00 mta1 postfix/smtp[2707]: 6DBFF1456C: to=<old.test@example.org>, "
    "relay=mx.example.org[192.0.2.10]:25, delay=0.18, delays=0.15/0.02/0/0.01, dsn=5.1.1, "
    "status=bounced (host mx.example.org[192.0.2.10] said: 550 5.1.1 User unknown "
    "(in reply to RCPT TO command))"
)
NO_MX = (
    "2026-11-09T02:14:19+00:00 mta1 postfix/smtp[2708]: 6DBFF1456E: to=<ana@nowhere.example>, "
    "relay=none, delay=0.1, delays=0.1/0/0/0, dsn=5.4.4, status=bounced "
    "(Host or domain name not found. Name service error for name=nowhere.example type=AAAA)"
)
HOP_TO_OWN_NODE = (
    "2026-11-09T02:14:19+00:00 mta1 postfix/smtp[2709]: 6DBFF1456D: to=<ana@example.org>, "
    "relay=node1.mail.example.com[203.0.113.20]:25, delay=0.2, delays=0.1/0/0/0.1, dsn=5.0.0, "
    "status=bounced (host node1.mail.example.com[203.0.113.20] said: 550 5.0.0 refused)"
)


@contextmanager
def _install_suffix():
    previous = app._INTERNAL_RELAY_SUFFIX
    app._INTERNAL_RELAY_SUFFIX = SUFFIX
    try:
        yield
    finally:
        app._INTERNAL_RELAY_SUFFIX = previous


def test_refusal_by_the_recipient_mx_counts_as_a_bounce():
    host = app.bounce_relay_host(REMOTE_REFUSAL)
    assert host == "mx.example.org"
    with _install_suffix():
        assert app.should_ignore_bounce("old.test@example.org", host, "postfix/smtp", "6DBFF1456C") is False


def test_bounce_without_a_relay_counts_as_a_bounce():
    host = app.bounce_relay_host(NO_MX)
    assert host == "none"
    with _install_suffix():
        assert app.should_ignore_bounce("ana@nowhere.example", host, "postfix/smtp", "6DBFF1456E") is False


def test_refusal_by_one_of_our_own_nodes_is_an_internal_hop():
    host = app.bounce_relay_host(HOP_TO_OWN_NODE)
    assert host == "node1.mail.example.com"
    with _install_suffix():
        assert app.should_ignore_bounce("ana@example.org", host, "postfix/smtp", "6DBFF1456D") is True


def test_the_logging_node_is_not_the_hop():
    # the regression: the node that logged the line is internal by definition
    assert app.bounce_relay_host("mta1.mail.example.com postfix/smtp[1]: X: to=<a@b.example>") is None


def test_structured_event_relay_comes_from_the_grok_field_or_the_line():
    assert app.event_relay_host({"relay_name": "mx.example.org", "hostname": "mta1.mail.example.com"}) == "mx.example.org"
    assert app.event_relay_host({"relay": "none"}) == "none"
    assert app.event_relay_host({"message": REMOTE_REFUSAL}) == "mx.example.org"
    assert app.event_relay_host({"host": "mta1.mail.example.com", "status_msg": "host mx said: 550"}) is None
