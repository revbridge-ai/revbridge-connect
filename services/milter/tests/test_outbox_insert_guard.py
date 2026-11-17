"""Tests for `KafkaClient._insert_with_guard`.

`cur.lastrowid` is known before `conn.commit()`, so logging success right
after the INSERT could report a row that a failed commit never persisted
(an auto-increment ID used up with no row behind it).

These tests pin the guard's three invariants:

  1. Happy path: INSERT + commit + SELECT-after-INSERT all succeed
     → return outbox_id, no warning logs.
  2. Ghost row: INSERT lastrowid is captured but SELECT-after-INSERT
     returns nothing → the guard logs OUTBOX_GHOST and retries once.
  3. Commit failure: commit raises → the guard logs
     OUTBOX_COMMIT_FAIL and retries once.

The tests stub `get_conn` so we never touch a real MySQL: the goal is
to verify the guard's *control flow*, not the SQL. Reproducing the
DB-level condition itself needs an integration test against a real MySQL.
"""
from __future__ import annotations

from contextlib import contextmanager
from unittest.mock import MagicMock, patch


class _FakeCursor:
    """Records executes; configurable lastrowid + fetchone result."""

    def __init__(self, *, lastrowid: int = 1, fetchone_result=None):
        self.lastrowid = lastrowid
        self._fetchone_result = fetchone_result
        self.executed: list[tuple] = []

    def execute(self, sql, params=None):
        self.executed.append((sql, params))

    def fetchone(self):
        return self._fetchone_result

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


class _FakeConn:
    """Yields configured cursors in order; tracks commit calls/failures."""

    def __init__(
        self,
        *,
        insert_lastrowid: int,
        verify_row,
        commit_raises: Exception | None = None,
    ):
        # First cursor() is for the INSERT, second is the verify SELECT.
        self._cursors = [
            _FakeCursor(lastrowid=insert_lastrowid),
            _FakeCursor(fetchone_result=verify_row),
        ]
        self._commit_raises = commit_raises
        self.commits = 0

    def cursor(self):
        return self._cursors.pop(0)

    def commit(self):
        self.commits += 1
        if self._commit_raises is not None:
            err = self._commit_raises
            self._commit_raises = None  # only raise once, second attempt clean
            raise err


@contextmanager
def _fake_get_conn(conn: _FakeConn):
    yield conn


def _make_client():
    """Build a KafkaClient stub with logger attached. Importing the
    module triggers side-effects (connect_lib, etc.), so we do it
    lazily inside the test."""
    from mail_milter.common.kafka_client import KafkaClient

    client = KafkaClient.__new__(KafkaClient)  # bypass __init__
    client.enabled = True
    client.logger = MagicMock()
    return client


# --------------------- Happy path -------------------------------------


def test_insert_persists_returns_outbox_id():
    """Standard success: INSERT, commit OK, SELECT finds row → return id."""
    client = _make_client()

    conn = _FakeConn(insert_lastrowid=42, verify_row=(42,))

    with patch("mail_milter.common.kafka_client.get_conn", lambda: _fake_get_conn(conn)):
        result = client._insert_with_guard(
            sql="INSERT ...",
            params=("sent", "<msg-1@x>", "u@x.com", "x.com", "{}"),
            event_type="sent",
            provider_event_id="<msg-1@x>",
            rcpt="u@x.com",
        )

    assert result == 42
    assert conn.commits == 1
    # No error/warning logs on the happy path.
    client.logger.error.assert_not_called()
    client.logger.warning.assert_not_called()


# --------------------- Ghost row ----------------------------------------


def test_ghost_row_logs_and_retries_once():
    """Auto-increment id reserved but SELECT finds nothing → log
    OUTBOX_GHOST and retry. Second attempt persists → return id."""
    client = _make_client()

    # First attempt: lastrowid=100, but SELECT-after-INSERT returns None
    # (the row did not persist after the commit).
    conn1 = _FakeConn(insert_lastrowid=100, verify_row=None)
    # Second attempt (retry): clean success.
    conn2 = _FakeConn(insert_lastrowid=101, verify_row=(101,))
    conns = iter([conn1, conn2])

    with patch(
        "mail_milter.common.kafka_client.get_conn",
        lambda: _fake_get_conn(next(conns)),
    ):
        result = client._insert_with_guard(
            sql="INSERT ...",
            params=("sent", "<msg-2@x>", "u2@x.com", "x.com", "{}"),
            event_type="sent",
            provider_event_id="<msg-2@x>",
            rcpt="u2@x.com",
        )

    # Retry succeeded → return the id from attempt 2.
    assert result == 101
    # The ghost was logged loudly.
    ghost_calls = [
        c for c in client.logger.error.call_args_list
        if "OUTBOX_GHOST" in str(c)
    ]
    assert len(ghost_calls) == 1


def test_ghost_row_twice_returns_none_and_logs_permanent():
    """Both attempts ghost out → permanent failure, return None."""
    client = _make_client()

    conn1 = _FakeConn(insert_lastrowid=200, verify_row=None)
    conn2 = _FakeConn(insert_lastrowid=201, verify_row=None)
    conns = iter([conn1, conn2])

    with patch(
        "mail_milter.common.kafka_client.get_conn",
        lambda: _fake_get_conn(next(conns)),
    ):
        result = client._insert_with_guard(
            sql="INSERT ...",
            params=("sent", "<msg-3@x>", "u3@x.com", "x.com", "{}"),
            event_type="sent",
            provider_event_id="<msg-3@x>",
            rcpt="u3@x.com",
        )

    assert result is None
    # 2 ghost logs + 1 permanent-failure log.
    error_msgs = [str(c) for c in client.logger.error.call_args_list]
    assert sum("OUTBOX_GHOST" in m for m in error_msgs) == 2
    assert sum("OUTBOX_PERMANENT_FAIL" in m for m in error_msgs) == 1


# --------------------- Commit failure ---------------------------------


def test_commit_failure_logs_and_retries():
    """conn.commit() raises → OUTBOX_COMMIT_FAIL log, retry once."""
    client = _make_client()

    conn1 = _FakeConn(
        insert_lastrowid=300,
        verify_row=None,  # not reached on commit-fail
        commit_raises=RuntimeError("simulated deadlock"),
    )
    conn2 = _FakeConn(insert_lastrowid=301, verify_row=(301,))
    conns = iter([conn1, conn2])

    with patch(
        "mail_milter.common.kafka_client.get_conn",
        lambda: _fake_get_conn(next(conns)),
    ):
        result = client._insert_with_guard(
            sql="INSERT ...",
            params=("sent", "<msg-4@x>", "u4@x.com", "x.com", "{}"),
            event_type="sent",
            provider_event_id="<msg-4@x>",
            rcpt="u4@x.com",
        )

    assert result == 301
    error_msgs = [str(c) for c in client.logger.error.call_args_list]
    assert any("OUTBOX_COMMIT_FAIL" in m for m in error_msgs), error_msgs
    assert any("simulated deadlock" in m for m in error_msgs)
