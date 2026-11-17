"""send_ledger DAO: the durable source of truth on the SENDING side.

Lifecycle: admit (queued) → atomic pre-SMTP claim (sending) → terminal
(smtp_accepted | failed | deadletter). Correlated with `deliveries`
(milter) by (message_id, rcpt).

PRINCIPLE: every operation is best-effort / FAIL-OPEN. A MySQL failure in the
ledger must NEVER stop a send: the ledger is additive. With a healthy database
the claim is a strong dedupe guarantee (no double send). With the database
down, sending continues without the ledger, so the ledger can never make
delivery worse than sending without it.

Table: config/db/initdb/023_mail_send_ledger.sql
"""
import logging

from mail_api.common.db import (
    exec_sql_async,
    exec_sql_rowcount_async,
    fetch_all_async,
)

logger = logging.getLogger(__name__)

DEFAULT_MAX_ATTEMPTS = 3
_ERR_MAX = 1000


def _trunc(value, n: int = _ERR_MAX):
    if value is None:
        return None
    s = str(value)
    return s[:n]


def _norm_rcpt(rcpt):
    """Normalizes rcpt at the DAO boundary. The milter and the events updater
    store addresses in lowercase, so a raw address with uppercase letters would
    create a second ledger row that never reaches delivered."""
    return str(rcpt).strip().lower() if rcpt else rcpt


async def admit(
    message_id,
    rcpt,
    domain_name,
    domain_id=None,
    customer_id=None,
    idempotency_key=None,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
):
    """Records that a (message_id, rcpt) was admitted by /send (state=queued).

    Idempotent through INSERT IGNORE on UNIQUE(message_id, rcpt): admitting the
    same pair again neither duplicates nor overwrites it. Best-effort.
    """
    rcpt = _norm_rcpt(rcpt)
    sql = (
        "INSERT IGNORE INTO send_ledger "
        "(message_id, rcpt, domain_name, domain_id, customer_id, idempotency_key, "
        " state, max_attempts) "
        "VALUES (%s, %s, %s, %s, %s, %s, 'queued', %s)"
    )
    try:
        await exec_sql_async(
            sql,
            (message_id, rcpt, domain_name, domain_id, customer_id,
             idempotency_key, max_attempts),
        )
    except Exception as e:
        logger.error("[LEDGER] admit failed %s->%s: %s", message_id, rcpt, e)


async def claim(message_id, rcpt, domain_name, worker_id, lease_sec) -> bool:
    """Atomic pre-SMTP claim. Returns True if THIS worker should send.

    1) INSERT IGNORE makes sure the row exists (items queued before the ledger
       existed, or the window between enqueue and admit).
    2) The conditional UPDATE settles the race: only one transaction flips
       queued→sending (row lock on the UNIQUE key). rowcount==1 → this worker won.

    FAIL-OPEN: any database error returns True (send anyway; only the dedupe
    guarantee for that item is lost).
    """
    rcpt = _norm_rcpt(rcpt)
    try:
        await exec_sql_async(
            "INSERT IGNORE INTO send_ledger (message_id, rcpt, domain_name, state) "
            "VALUES (%s, %s, %s, 'queued')",
            (message_id, rcpt, domain_name),
        )
    except Exception as e:
        logger.error("[LEDGER] claim/ensure-row failed %s->%s: %s (fail-open)",
                     message_id, rcpt, e)
        return True

    sql = (
        "UPDATE send_ledger "
        "SET state='sending', worker_id=%s, attempt=attempt+1, claimed_at=NOW(), "
        "    lease_expires_at=NOW() + INTERVAL %s SECOND "
        "WHERE message_id=%s AND rcpt=%s "
        "  AND (state='queued' OR (state='sending' AND lease_expires_at < NOW()))"
    )
    try:
        rc = await exec_sql_rowcount_async(
            sql, (worker_id, int(lease_sec), message_id, rcpt)
        )
        return bool(rc and rc > 0)
    except Exception as e:
        logger.error("[LEDGER] claim failed %s->%s: %s (fail-open)",
                     message_id, rcpt, e)
        return True


async def mark_accepted(message_id, rcpt, delivery_deadline_sec=None):
    """Postfix/the relay accepted the submission (2xx). This is NOT the real
    terminal state: the message still sits in the Postfix queue. delivery_deadline
    arms the reaper: if no delivery event (sent/deferred/bounced) arrives by then,
    the message was lost silently (for example, the MTA restarted and lost its
    queue) and will be re-injected. 'deferred' events PUSH the deadline forward
    (events updater), so a short deadline here does not duplicate a message that
    Postfix is still retrying."""
    rcpt = _norm_rcpt(rcpt)
    if delivery_deadline_sec is None:
        delivery_deadline_sec = DEFAULT_DELIVERY_DEADLINE_SEC
    sql = (
        "UPDATE send_ledger SET state='smtp_accepted', terminal_at=NOW(), "
        "lease_expires_at=NULL, "
        "delivery_deadline=NOW() + INTERVAL %s SECOND "
        "WHERE message_id=%s AND rcpt=%s"
    )
    try:
        await exec_sql_async(sql, (int(delivery_deadline_sec), message_id, rcpt))
    except Exception as e:
        logger.error("[LEDGER] mark_accepted failed %s->%s: %s", message_id, rcpt, e)


async def mark_failed(message_id, rcpt, error=None, smtp_code=None):
    """Permanent failure (5xx, invalid payload): terminal."""
    rcpt = _norm_rcpt(rcpt)
    sql = (
        "UPDATE send_ledger SET state='failed', terminal_at=NOW(), "
        "lease_expires_at=NULL, last_error=%s, last_smtp_code=%s "
        "WHERE message_id=%s AND rcpt=%s"
    )
    try:
        await exec_sql_async(sql, (_trunc(error), smtp_code, message_id, rcpt))
    except Exception as e:
        logger.error("[LEDGER] mark_failed failed %s->%s: %s", message_id, rcpt, e)


async def mark_requeued(message_id, rcpt, error=None):
    """Recoverable deferral: back to queued so it can be claimed again later."""
    rcpt = _norm_rcpt(rcpt)
    sql = (
        "UPDATE send_ledger SET state='queued', worker_id=NULL, "
        "lease_expires_at=NULL, last_error=%s WHERE message_id=%s AND rcpt=%s"
    )
    try:
        await exec_sql_async(sql, (_trunc(error), message_id, rcpt))
    except Exception as e:
        logger.error("[LEDGER] mark_requeued failed %s->%s: %s", message_id, rcpt, e)


async def mark_deadletter(message_id, rcpt, error=None):
    """Ran out of attempts on temporary failures: terminal, needs an operator."""
    rcpt = _norm_rcpt(rcpt)
    sql = (
        "UPDATE send_ledger SET state='deadletter', terminal_at=NOW(), "
        "lease_expires_at=NULL, next_attempt_at=NULL, last_error=%s "
        "WHERE message_id=%s AND rcpt=%s"
    )
    try:
        await exec_sql_async(sql, (_trunc(error), message_id, rcpt))
    except Exception as e:
        logger.error("[LEDGER] mark_deadletter failed %s->%s: %s", message_id, rcpt, e)


# ---------------------------------------------------------------------------
# Closing the durability loop: reconciler (events updater) + reaper
# (workers/ledger_reaper.py).
# ---------------------------------------------------------------------------

# How long SOME delivery event (sent/deferred/bounced) may take to arrive before
# the reaper treats the message as lost. It does NOT need to cover Postfix's own
# retries: every 'deferred' event PUSHES the deadline forward (events updater),
# so the deadline only expires when Postfix went silent about the message.
# 3600 (not 600): it must be much larger than the event pipeline lag during a
# campaign burst, otherwise the reaper declares a delivered message lost and
# sends it twice (see config.LEDGER_DELIVERY_DEADLINE_SEC).
DEFAULT_DELIVERY_DEADLINE_SEC = 3600  # 1h


async def mark_delivered(message_id, rcpt):
    """REAL delivery confirmed by the relay (status=sent, from a delivery event):
    the TRUE terminal state. Only moves from non-terminal states (it does not
    revive failed/deadletter or mark a delivered row again)."""
    rcpt = _norm_rcpt(rcpt)
    sql = (
        "UPDATE send_ledger SET state='delivered', delivered_at=NOW(), "
        "terminal_at=NOW(), lease_expires_at=NULL, next_attempt_at=NULL "
        "WHERE message_id=%s AND rcpt=%s "
        "  AND state IN ('queued','sending','smtp_accepted','deferred')"
    )
    try:
        await exec_sql_async(sql, (message_id, rcpt))
    except Exception as e:
        logger.error("[LEDGER] mark_delivered failed %s->%s: %s", message_id, rcpt, e)


async def mark_deferred(message_id, rcpt, backoff_sec: int, error=None):
    """The relay/Postfix deferred the message in a recoverable way. Schedules the
    reaper's re-injection (the ledger IS the durable deferred queue) with
    next_attempt_at = now + backoff."""
    rcpt = _norm_rcpt(rcpt)
    sql = (
        "UPDATE send_ledger SET state='deferred', last_error=%s, "
        "next_attempt_at=NOW() + INTERVAL %s SECOND, "
        "lease_expires_at=NULL, delivery_deadline=NULL "
        "WHERE message_id=%s AND rcpt=%s "
        "  AND state IN ('sending','smtp_accepted','queued')"
    )
    try:
        await exec_sql_async(sql, (_trunc(error), int(backoff_sec), message_id, rcpt))
    except Exception as e:
        logger.error("[LEDGER] mark_deferred failed %s->%s: %s", message_id, rcpt, e)


async def has_confirmed_delivery(message_id, rcpt) -> bool:
    """Reaper safety belt: True if `deliveries` OR `delivery_events` already
    records a confirmed delivery (sent/delivered) for (message_id, rcpt).

    `delivery_events` is part of the query because `deliveries` only moves in the
    events updater's real-time path, which can lag by minutes during a campaign
    burst, while the history table receives the event within seconds. Checking
    both keeps the reaper from re-injecting a message that was already delivered.
    Uses the prefix of the unique_delivery_event index (message_id, rcpt, status)."""
    rcpt = _norm_rcpt(rcpt)
    sql = (
        "SELECT 1 FROM deliveries WHERE message_id=%s AND rcpt=%s "
        "AND status IN ('sent','delivered') "
        "UNION ALL "
        "SELECT 1 FROM delivery_events WHERE message_id=%s AND rcpt=%s "
        "AND status IN ('sent','delivered') "
        "LIMIT 1"
    )
    try:
        rows = await fetch_all_async(sql, (message_id, rcpt, message_id, rcpt))
        return bool(rows)
    except Exception as e:
        logger.error("[LEDGER] has_confirmed_delivery failed %s->%s: %s",
                     message_id, rcpt, e)
        return False


async def sweep_lost(limit: int = 200):
    """Reaper: SILENT LOSS.

    Rows that Postfix accepted (smtp_accepted) but whose delivery event never
    arrived before delivery_deadline: Postfix probably lost the message (restart,
    out of memory) and logged no 'deferred' event. Also picks up 'sending' rows
    with an expired lease (the worker died before the 250). Returns the rows to
    re-inject; does NOT change state here (the reaper decides and re-injects)."""
    sql = (
        "SELECT message_id, rcpt, domain_name, domain_id, customer_id, "
        "       attempt, max_attempts, queue_id "
        "FROM send_ledger "
        "WHERE (state='smtp_accepted' AND delivery_deadline IS NOT NULL "
        "         AND delivery_deadline < NOW()) "
        "   OR (state='sending' AND lease_expires_at IS NOT NULL "
        "         AND lease_expires_at < NOW()) "
        "ORDER BY admitted_at ASC LIMIT %s"
    )
    try:
        rows = await fetch_all_async(sql, (int(limit),))
        return [dict(r) for r in rows] if rows else []
    except Exception as e:
        logger.error("[LEDGER] sweep_lost failed: %s", e)
        return []


async def sweep_stale_queued(grace_sec: int, limit: int = 200):
    """Reaper: ORPHANED 'queued' rows, admitted more than grace_sec ago and never
    claimed (attempt=0). This happens when the API process is killed in the middle
    of the enqueue loop (between the admit and the RPUSH): the idempotency key
    stays claimed, the caller's retry gets the cached 'queued' answer and the
    recipient never reaches the queue. The CALLER must make sure the Redis queue
    is EMPTY before using this: with a backlog, old queued rows are normal.
    """
    sql = (
        "SELECT message_id, rcpt, domain_name, domain_id, customer_id, "
        "       attempt, max_attempts, queue_id "
        "FROM send_ledger "
        "WHERE state='queued' AND attempt = 0 "
        "  AND admitted_at < NOW() - INTERVAL %s SECOND "
        "ORDER BY admitted_at ASC LIMIT %s"
    )
    try:
        rows = await fetch_all_async(sql, (int(grace_sec), int(limit)))
        return [dict(r) for r in rows] if rows else []
    except Exception as e:
        logger.error("[LEDGER] sweep_stale_queued failed: %s", e)
        return []


async def sweep_due(limit: int = 200):
    """Reaper: SCHEDULED deferrals. Rows in 'deferred' whose next_attempt_at has
    passed → re-inject now (honoring the backoff already stored)."""
    sql = (
        "SELECT message_id, rcpt, domain_name, domain_id, customer_id, "
        "       attempt, max_attempts, queue_id "
        "FROM send_ledger "
        "WHERE state='deferred' AND next_attempt_at IS NOT NULL "
        "  AND next_attempt_at < NOW() "
        "ORDER BY next_attempt_at ASC LIMIT %s"
    )
    try:
        rows = await fetch_all_async(sql, (int(limit),))
        return [dict(r) for r in rows] if rows else []
    except Exception as e:
        logger.error("[LEDGER] sweep_due failed: %s", e)
        return []
