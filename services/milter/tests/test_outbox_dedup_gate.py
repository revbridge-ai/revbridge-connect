"""Tests for the outbox dedup gate in `process_delivery_event`.

The same Postfix line can be processed more than once (a restart, or Vector
re-POSTing a batch), so the outbox enqueue is gated on an actual status
transition. These tests pin that behaviour:

  - a real transition (None/sent/deferred -> delivered) enqueues exactly once;
  - reprocessing a status already persisted enqueues nothing.

External deps (DB, Redis, domain lookup, status mapping) are stubbed so the
test targets only the gate's control flow, not SQL or status parsing.
"""
from __future__ import annotations

from contextlib import contextmanager
from unittest.mock import MagicMock, patch

_APP = "mail_milter.events_updater.app"


def _drive(*, current_status, mapped_status):
    """Run process_delivery_event with deps stubbed; return (trigger, exec_sql) mocks.

    `fetch_one` feeds the pre-upsert current_status (None = no prior row), still
    used by the cheap FAST_PATH early-out; `map_delivery_status` is pinned so the
    test exercises only the gate/fast-path. The authoritative "did the status
    change?" decision now comes from `upsert_delivery_returning_changed` (the atomic
    UPSERT) — stubbed True here so a transition reaches the enqueue. `trigger` =
    outbox enqueue mock; `exec_sql` = the remaining DB write (delivery_events insert)
    — both must be skipped when a reprocess is short-circuited by the FAST_PATH.
    """

    def _fetch_one(sql, params=None):
        if "FROM deliveries" in sql and current_status is not None:
            return {"status": current_status}
        return None

    redis_mock = MagicMock()
    redis_mock.get.return_value = None
    redis_mock.scan_keys.return_value = []

    with patch(f"{_APP}.fetch_one", side_effect=_fetch_one), \
         patch(f"{_APP}.fetch_all", return_value=[]), \
         patch(f"{_APP}.exec_sql", return_value=None) as exec_sql, \
         patch(f"{_APP}.upsert_delivery_returning_changed", return_value=True), \
         patch(f"{_APP}.get_domain_id", return_value=1), \
         patch(f"{_APP}.map_delivery_status", return_value=mapped_status), \
         patch(f"{_APP}.redis", redis_mock), \
         patch(f"{_APP}.reconcile_send_ledger_delivered") as rec_delivered, \
         patch(f"{_APP}.trigger_kafka_event") as trigger:
        from mail_milter.events_updater.app import process_delivery_event

        result = process_delivery_event(
            message_id="<msg-1@mail.example.com>",
            rcpt="user@example.com",
            queue_id="ABC123",
            status="sent",
            dsn="2.0.0",
            host="mx.example.com",
            service="smtp",
            timestamp="2026-05-27T12:00:00Z",
        )
    return trigger, exec_sql, result, rec_delivered


def test_new_delivered_enqueues_once():
    """First delivery (no prior row) is a transition -> enqueue once + write."""
    trigger, exec_sql, _, _ = _drive(current_status=None, mapped_status="delivered")
    assert trigger.call_count == 1
    assert exec_sql.called


def test_transition_deferred_to_delivered_enqueues_once():
    """deferred -> delivered is a real transition -> enqueue once + write."""
    trigger, exec_sql, _, _ = _drive(current_status="deferred", mapped_status="delivered")
    assert trigger.call_count == 1
    assert exec_sql.called


def test_reprocessed_delivered_is_short_circuited():
    """Row already 'delivered', same line reprocessed -> NO enqueue, NO
    delivery_events write. The send_ledger reconcile still RUNS (idempotent), so
    a row that the reaper returned to 'queued' is closed when the delivered
    event arrives as a reprocess."""
    trigger, exec_sql, result, rec_delivered = _drive(
        current_status="delivered", mapped_status="delivered"
    )
    assert trigger.call_count == 0
    assert not exec_sql.called
    assert rec_delivered.call_count == 1
    assert result["status"] == "success" and result.get("skipped") is True


def test_reprocessed_deferred_is_short_circuited():
    """Row already 'deferred', deferred retry reprocessed -> NO enqueue, NO DB
    write, and no delivered reconcile (deferred is not terminal)."""
    trigger, exec_sql, result, rec_delivered = _drive(
        current_status="deferred", mapped_status="deferred"
    )
    assert trigger.call_count == 0
    assert not exec_sql.called
    assert rec_delivered.call_count == 0
    assert result.get("skipped") is True


# --- upsert_delivery_returning_changed: the atomic gate's rowcount logic ---------
#
# This is what makes the dedup race-proof: concurrent reprocesses of the same
# (message_id, rcpt) serialize on the UNIQUE KEY and only one sees changed=True.
# The decision is derived purely from MySQL's affected-rows, exercised here by
# driving the two statements' rowcounts.

def _run_helper(rowcounts):
    """Call the helper with get_conn() stubbed; the cursor yields `rowcounts` in
    order, one value per execute(). Returns (changed, cursor_mock)."""
    cur = MagicMock()
    seq = iter(rowcounts)

    def _execute(sql, params=None):
        cur.rowcount = next(seq)

    cur.execute.side_effect = _execute

    @contextmanager
    def _cursor_cm(*args, **kwargs):
        yield cur

    conn = MagicMock()
    conn.cursor.side_effect = _cursor_cm

    @contextmanager
    def _conn_cm():
        yield conn

    with patch(f"{_APP}.get_conn", _conn_cm):
        from mail_milter.events_updater.app import upsert_delivery_returning_changed

        changed = upsert_delivery_returning_changed(
            "<msg-1@mail.example.com>", "user@example.com", "example.com", 1, "delivered"
        )
    return changed, cur


def test_helper_real_transition_returns_true():
    """UPDATE matched an existing row whose status differed (rowcount 1) -> changed;
    the INSERT fallback is never run."""
    changed, cur = _run_helper([1])
    assert changed is True
    assert cur.execute.call_count == 1


def test_helper_first_seen_insert_returns_true():
    """No existing row (UPDATE rowcount 0) then INSERT created it (rowcount 1) ->
    first-seen transition -> changed."""
    changed, cur = _run_helper([0, 1])
    assert changed is True
    assert cur.execute.call_count == 2


def test_helper_reprocess_same_status_returns_false():
    """No transition (UPDATE rowcount 0) and INSERT was a no-op on the existing row
    (rowcount 0) -> a duplicate of an already-persisted status -> NOT changed.
    This is the call that a racing second worker makes; it must NOT enqueue."""
    changed, cur = _run_helper([0, 0])
    assert changed is False
    assert cur.execute.call_count == 2
