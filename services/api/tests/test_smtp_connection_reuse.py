"""
Tests for the reusable SMTP connection pool (feature flag SMTP_CONNECTION_REUSE).

Main focus: prove the ISOLATION BETWEEN CUSTOMERS. An SMTP connection is born
authenticated as ONE customer (a specific smtp_user/relay); the pool may only reuse it
for messages with the identical identity key
(smtp_host, smtp_port, secure, starttls, smtp_user). A message from another customer =>
different key => the old connection is closed and a new one is opened. Nothing is ever
sent cross-customer over a connection authenticated as someone else.

Also covers: reuse for the same customer, recycling at the message cap,
filtering of dead sockets at checkout (NOOP), and transparent reopening when the
socket drops in the middle of a send.
"""

import smtplib

import mail_api.common.config as config_mod
import mail_api.services.email_service as es
import pytest
from mail_api.services.email_service import MailService


class FakeSMTP:
    """Fake SMTP connection that records logins/sends and simulates controlled failures."""

    instances: list["FakeSMTP"] = []

    def __init__(self, host, port, context=None, **kwargs):
        self.host = host
        self.port = port
        self.logged_in_as = None
        self.sent_to = []  # list of to_addrs per send_message
        self.closed = False
        self.alive = True  # if False, NOOP/send_message raise SMTPServerDisconnected
        self.fail_next_send = False  # fails the next send_message once
        FakeSMTP.instances.append(self)

    def starttls(self, context=None):
        return (220, b"ready")

    def login(self, user, password):
        self.logged_in_as = user

    def noop(self):
        if not self.alive:
            raise smtplib.SMTPServerDisconnected("noop on dead socket")
        return (250, b"OK")

    # email_service passes the envelope-from explicitly
    # (send_message(msg, from_addr=..., to_addrs=[...])), see the Return-Path
    # rbm<id>.<domain>. The fake must accept it, or it raises TypeError.
    def send_message(self, msg, from_addr=None, to_addrs=None):
        self.last_from_addr = from_addr
        if not self.alive:
            raise smtplib.SMTPServerDisconnected("send on dead socket")
        if self.fail_next_send:
            self.fail_next_send = False
            raise smtplib.SMTPServerDisconnected("server closed connection")
        self.sent_to.append(to_addrs)
        return {}

    def quit(self):
        self.closed = True

    def close(self):
        self.closed = True


# Identity map per customer: from_address -> (host, port, user)
_CUSTOMERS = {
    "campaign@customer-a.example": ("smtp-a.example.net", 587, "ak_customer_a"),
    "campaign@customer-b.example": ("smtp-b.example.net", 587, "ak_customer_b"),
}


@pytest.fixture
def reuse_env(monkeypatch):
    """Turns reuse on and installs the config/credentials/smtplib fakes."""
    FakeSMTP.instances.clear()

    monkeypatch.setattr(es, "SMTP_CONNECTION_REUSE", True)
    monkeypatch.setattr(es, "SMTP_REUSE_MAX_MESSAGES", 50)
    monkeypatch.setattr(es, "SMTP_REUSE_IDLE_TIMEOUT_SEC", 30.0)

    async def fake_get_config(from_email, domain_id=None, use_cache=True):
        host, port, _user = _CUSTOMERS[from_email]
        return {
            "smtp_host": host,
            "smtp_port": port,
            "smtp_secure": False,
            "smtp_use_starttls": False,
        }

    async def fake_get_creds(from_email, api_key=None):
        _host, _port, user = _CUSTOMERS[from_email]
        return user, "secret-password"

    # get_smtp_config_for_email is imported INSIDE send(), from config
    monkeypatch.setattr(config_mod, "get_smtp_config_for_email", fake_get_config)
    # get_smtp_credentials_for_email is imported at the top of the email_service module
    monkeypatch.setattr(es, "get_smtp_credentials_for_email", fake_get_creds)
    monkeypatch.setattr(es.smtplib, "SMTP", FakeSMTP)
    monkeypatch.setattr(es.smtplib, "SMTP_SSL", FakeSMTP)

    return monkeypatch


def _mail(from_address, to_address):
    return {
        "headers": {},
        "from": {"name": "Campaign", "address": from_address},
        "to": {"name": "Recipient", "address": to_address},
        "subject": "Test subject",
        "content": [{"type": "text/html", "value": "<p>Hello</p>"}],
    }


@pytest.mark.asyncio
async def test_same_customer_reuses_single_connection(reuse_env):
    svc = MailService()
    a = "campaign@customer-a.example"

    await svc.send(_mail(a, "u1@example.org"))
    await svc.send(_mail(a, "u2@example.org"))
    await svc.send(_mail(a, "u3@example.org"))

    # A single connection opened, reused for the 3 sends.
    assert len(FakeSMTP.instances) == 1
    conn = FakeSMTP.instances[0]
    assert conn.logged_in_as == "ak_customer_a"
    assert conn.sent_to == [["u1@example.org"], ["u2@example.org"], ["u3@example.org"]]
    assert conn.closed is False  # stays open in the pool


@pytest.mark.asyncio
async def test_different_customer_never_reuses_connection(reuse_env):
    """ISOLATION: mail from one customer NEVER goes out over a connection
    authenticated as another.

    The invariant is ISOLATION, not the connection count: the pool is a dict
    keyed by customer (email_service._pool), so A's connection may stay
    open while B uses its own — A->B->A uses 2 connections, not 3. The test
    therefore asserts isolation and complete delivery, not a fixed number of
    connections.
    """
    svc = MailService()
    a = "campaign@customer-a.example"
    b = "campaign@customer-b.example"

    await svc.send(_mail(a, "a1@example.org"))
    await svc.send(_mail(b, "b1@example.org"))  # a different customer
    await svc.send(_mail(a, "a2@example.org"))  # back to A

    # Every connection is authenticated as ONE customer and only sent that customer's mail.
    for conn in FakeSMTP.instances:
        rcpts = [r for lote in conn.sent_to for r in lote]
        esperado = {"ak_customer_a": "a", "ak_customer_b": "b"}[conn.logged_in_as]
        assert all(r.startswith(esperado) for r in rcpts), (
            f"cross-customer: connection {conn.logged_in_as} sent {rcpts}"
        )

    # And each customer had ALL of its mail delivered, in order.
    por_login: dict[str, list[str]] = {}
    for conn in FakeSMTP.instances:
        por_login.setdefault(conn.logged_in_as, []).extend(
            r for lote in conn.sent_to for r in lote
        )
    assert por_login["ak_customer_a"] == ["a1@example.org", "a2@example.org"]
    assert por_login["ak_customer_b"] == ["b1@example.org"]


async def test_recycles_connection_after_max_messages(reuse_env):
    reuse_env.setattr(es, "SMTP_REUSE_MAX_MESSAGES", 2)
    svc = MailService()
    a = "campaign@customer-a.example"

    await svc.send(_mail(a, "u1@example.org"))
    await svc.send(_mail(a, "u2@example.org"))  # conn1 reaches the cap (2)
    await svc.send(_mail(a, "u3@example.org"))  # checkout recycles -> conn2

    assert len(FakeSMTP.instances) == 2
    assert FakeSMTP.instances[0].closed is True
    assert FakeSMTP.instances[0].sent_to == [["u1@example.org"], ["u2@example.org"]]
    assert FakeSMTP.instances[1].sent_to == [["u3@example.org"]]


@pytest.mark.asyncio
async def test_dead_socket_filtered_at_checkout(reuse_env):
    """A socket closed by the server between sends is detected by the NOOP at checkout
    and replaced by a new connection (without losing the email)."""
    svc = MailService()
    a = "campaign@customer-a.example"

    await svc.send(_mail(a, "u1@example.org"))
    FakeSMTP.instances[0].alive = False  # the server dropped the idle connection

    await svc.send(_mail(a, "u2@example.org"))

    assert len(FakeSMTP.instances) == 2
    assert FakeSMTP.instances[0].closed is True
    assert FakeSMTP.instances[1].sent_to == [["u2@example.org"]]


@pytest.mark.asyncio
async def test_reopens_transparently_when_send_fails_on_reused(reuse_env):
    """The NOOP passes but send_message fails (the socket dies during the send): the
    reused connection is reopened ONCE and the email resent, transparently."""
    svc = MailService()
    a = "campaign@customer-a.example"

    await svc.send(_mail(a, "u1@example.org"))
    # conn1 survives the NOOP but fails the next send once.
    FakeSMTP.instances[0].fail_next_send = True

    await svc.send(_mail(a, "u2@example.org"))

    assert len(FakeSMTP.instances) == 2
    assert FakeSMTP.instances[0].closed is True  # discarded after the failure
    assert FakeSMTP.instances[1].sent_to == [["u2@example.org"]]  # resent on the new one


@pytest.mark.asyncio
async def test_reuse_disabled_closes_every_connection(monkeypatch):
    """With the flag OFF (default), the default behavior holds: one connection per
    email, closed after each send."""
    FakeSMTP.instances.clear()
    monkeypatch.setattr(es, "SMTP_CONNECTION_REUSE", False)

    async def fake_get_config(from_email, domain_id=None, use_cache=True):
        host, port, _user = _CUSTOMERS[from_email]
        return {"smtp_host": host, "smtp_port": port, "smtp_secure": False, "smtp_use_starttls": False}

    async def fake_get_creds(from_email, api_key=None):
        _host, _port, user = _CUSTOMERS[from_email]
        return user, "secret-password"

    monkeypatch.setattr(config_mod, "get_smtp_config_for_email", fake_get_config)
    monkeypatch.setattr(es, "get_smtp_credentials_for_email", fake_get_creds)
    monkeypatch.setattr(es.smtplib, "SMTP", FakeSMTP)
    monkeypatch.setattr(es.smtplib, "SMTP_SSL", FakeSMTP)

    svc = MailService()
    a = "campaign@customer-a.example"
    await svc.send(_mail(a, "u1@example.org"))
    await svc.send(_mail(a, "u2@example.org"))

    assert len(FakeSMTP.instances) == 2
    assert all(c.closed for c in FakeSMTP.instances)
