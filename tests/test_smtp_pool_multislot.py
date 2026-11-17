"""Multi-slot SMTP pool: connections for different keys coexist (required by fairness)."""
from __future__ import annotations

import pytest
from mail_api.services import email_service as es_mod
from mail_api.services.email_service import MailService


class _FakeConn:
    def __init__(self):
        self.quit_called = False

    def noop(self):
        return (250, b"ok")

    def quit(self):
        self.quit_called = True

    def close(self):
        pass


@pytest.fixture
def svc(monkeypatch):
    monkeypatch.setattr(es_mod, "SMTP_CONNECTION_REUSE", True)
    return MailService()


KEY_A = ("hostA", 587, False, True, "userA")
KEY_B = ("hostB", 587, False, True, "userB")


def test_two_customers_coexist(svc):
    """With fairness alternating between customers, both connections stay warm: with a
    single slot, B's checkin would destroy A's connection."""
    a, b = _FakeConn(), _FakeConn()
    svc._pool_checkin(KEY_A, a, 1)
    svc._pool_checkin(KEY_B, b, 1)
    got_a = svc._pool_checkout(KEY_A)
    got_b = svc._pool_checkout(KEY_B)
    assert got_a and got_a[0] is a and not a.quit_called
    assert got_b and got_b[0] is b and not b.quit_called


def test_lru_eviction_respects_cap(svc, monkeypatch):
    monkeypatch.setattr(es_mod, "SMTP_REUSE_MAX_CONNS", 2)
    conns = [_FakeConn() for _ in range(3)]
    svc._pool_checkin(("h1", 1, False, False, "u1"), conns[0], 1)
    svc._pool_checkin(("h2", 2, False, False, "u2"), conns[1], 1)
    svc._pool_checkin(("h3", 3, False, False, "u3"), conns[2], 1)
    # cap 2: the oldest one (conns[0]) was evicted with quit
    assert conns[0].quit_called
    assert not conns[1].quit_called and not conns[2].quit_called
    assert len(svc._pool) == 2


def test_max_messages_recycles_on_checkout(svc, monkeypatch):
    monkeypatch.setattr(es_mod, "SMTP_REUSE_MAX_MESSAGES", 2)
    conn = _FakeConn()
    svc._pool_checkin(KEY_A, conn, 2)  # already sent the maximum
    assert svc._pool_checkout(KEY_A) is None
    assert conn.quit_called
