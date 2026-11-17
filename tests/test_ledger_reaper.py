"""Unit tests for LedgerReaper: how it closes lost rows, with fake dependencies."""
from __future__ import annotations

import pytest
from mail_api.workers import ledger_reaper
from mail_api.workers.ledger_reaper import LedgerReaper


class _FakeRedis:
    def __init__(self):
        self.pushed: list[str] = []

    async def rpush(self, key, value):
        self.pushed.append(value)
        return 1


class _LedgerCalls:
    """Records the calls made to the ledger DAO."""

    def __init__(self, already_delivered=False):
        self.calls: list[tuple] = []
        self.DEFAULT_MAX_ATTEMPTS = 3
        self.already_delivered = already_delivered

    async def mark_deadletter(self, message_id, rcpt, error=None):
        self.calls.append(("deadletter", message_id, rcpt, error))

    async def mark_delivered(self, message_id, rcpt):
        self.calls.append(("delivered", message_id, rcpt))

    async def has_confirmed_delivery(self, message_id, rcpt):
        return self.already_delivered


def _row(**kw):
    base = {"message_id": "m1", "rcpt": "user@dest.com", "attempt": 1,
            "max_attempts": 3, "domain_name": "d.com", "domain_id": 1,
            "customer_id": "c1", "queue_id": None}
    base.update(kw)
    return base


@pytest.fixture
def wired(monkeypatch):
    ledger = _LedgerCalls()
    redis = _FakeRedis()
    monkeypatch.setattr(ledger_reaper, "send_ledger", ledger)
    monkeypatch.setattr(ledger_reaper, "redis_client", redis)
    return ledger, redis


@pytest.mark.asyncio
async def test_max_attempts_goes_deadletter(wired):
    ledger, redis = wired
    outcome = await LedgerReaper()._close(_row(attempt=3, max_attempts=3))
    assert outcome == "deadletter"
    assert ledger.calls[0][0] == "deadletter"
    assert redis.pushed == []


@pytest.mark.asyncio
async def test_lost_row_goes_deadletter_with_a_marker(wired):
    ledger, redis = wired
    outcome = await LedgerReaper()._close(_row())
    assert outcome == "deadletter"
    assert "no copy" in ledger.calls[0][3]
    assert redis.pushed == []


@pytest.mark.asyncio
async def test_already_delivered_reconciles(monkeypatch):
    """deliveries already confirms the message left -> the row closes as delivered."""
    ledger = _LedgerCalls(already_delivered=True)
    redis = _FakeRedis()
    monkeypatch.setattr(ledger_reaper, "send_ledger", ledger)
    monkeypatch.setattr(ledger_reaper, "redis_client", redis)
    outcome = await LedgerReaper()._close(_row())
    assert outcome == "reconciled"
    assert ledger.calls[0][0] == "delivered"
    assert redis.pushed == []
