"""Tests for the send_ledger DAO (atomic claim + fail-open + transitions)."""

from unittest.mock import AsyncMock

import mail_api.services.send_ledger as sl
import pytest


@pytest.mark.asyncio
async def test_claim_true_when_rowcount_positive(monkeypatch):
    """rowcount=1 on the conditional UPDATE → this worker won the claim."""
    monkeypatch.setattr(sl, "exec_sql_async", AsyncMock(return_value=None))
    monkeypatch.setattr(sl, "exec_sql_rowcount_async", AsyncMock(return_value=1))

    got = await sl.claim("m1", "a@example.org", "example.org", "worker:1", 300)
    assert got is True


@pytest.mark.asyncio
async def test_claim_false_when_rowcount_zero(monkeypatch):
    """rowcount=0 → another worker holds the claim or it is already terminal → no send (dedupe)."""
    monkeypatch.setattr(sl, "exec_sql_async", AsyncMock(return_value=None))
    monkeypatch.setattr(sl, "exec_sql_rowcount_async", AsyncMock(return_value=0))

    got = await sl.claim("m1", "a@example.org", "example.org", "worker:1", 300)
    assert got is False


@pytest.mark.asyncio
async def test_claim_fail_open_when_update_raises(monkeypatch):
    """DB error on the UPDATE → fail-open (True): delivery comes first, only the dedupe is lost."""
    monkeypatch.setattr(sl, "exec_sql_async", AsyncMock(return_value=None))
    monkeypatch.setattr(
        sl, "exec_sql_rowcount_async", AsyncMock(side_effect=RuntimeError("db down"))
    )

    got = await sl.claim("m1", "a@example.org", "example.org", "worker:1", 300)
    assert got is True


@pytest.mark.asyncio
async def test_claim_fail_open_when_ensure_row_raises(monkeypatch):
    """DB error on the INSERT IGNORE (ensure-row) → fail-open (True)."""
    monkeypatch.setattr(
        sl, "exec_sql_async", AsyncMock(side_effect=RuntimeError("db down"))
    )
    rc = AsyncMock(return_value=1)
    monkeypatch.setattr(sl, "exec_sql_rowcount_async", rc)

    got = await sl.claim("m1", "a@example.org", "example.org", "worker:1", 300)
    assert got is True
    rc.assert_not_called()  # never reaches the UPDATE


@pytest.mark.asyncio
async def test_admit_fail_open(monkeypatch):
    """admit never propagates an error (best-effort) — it must not block queueing."""
    monkeypatch.setattr(
        sl, "exec_sql_async", AsyncMock(side_effect=RuntimeError("db down"))
    )
    # Must not raise.
    await sl.admit("m1", "a@example.org", "example.org")


@pytest.mark.asyncio
async def test_mark_accepted_issues_smtp_accepted_update(monkeypatch):
    exec_mock = AsyncMock(return_value=None)
    monkeypatch.setattr(sl, "exec_sql_async", exec_mock)

    await sl.mark_accepted("m1", "a@example.org")

    exec_mock.assert_awaited_once()
    sql = exec_mock.await_args.args[0]
    assert "smtp_accepted" in sql
    # mark_accepted sets delivery_deadline (arms the reaper): the params are
    # (deadline_sec, mid, rcpt). The deadline value is not pinned here — the
    # default is config; the contract is the ORDER.
    params = exec_mock.await_args.args[1]
    assert params[1:] == ("m1", "a@example.org")
    assert isinstance(params[0], int) and params[0] > 0


@pytest.mark.asyncio
async def test_mark_requeued_returns_to_queued(monkeypatch):
    exec_mock = AsyncMock(return_value=None)
    monkeypatch.setattr(sl, "exec_sql_async", exec_mock)

    await sl.mark_requeued("m1", "a@example.org", "451 try later")

    sql = exec_mock.await_args.args[0]
    assert "state='queued'" in sql
