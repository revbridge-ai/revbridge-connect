-- =============================================================================
-- Schema: mail send ledger
-- Used by: services/api (the API and the send worker)
-- =============================================================================
-- DURABLE send ledger: the source of truth on the SENDING SIDE (admission -> atomic
-- pre-SMTP claim -> terminal state). Complements `deliveries`/`delivery_events`
-- (021_mail_milter.sql), which the milter only fills AFTER Postfix has accepted the
-- message. The ledger is the durable row that says "/send accepted this recipient",
-- which gives:
--   * at-least-once delivery (the Redis queue alone is at-most-once);
--   * pre-SMTP dedupe (two workers never send the same rcpt);
--   * a reliable reconciliation of what was accepted with what was delivered.
--
-- Correlates with `deliveries` by (message_id, rcpt), the same key.

-- =============================================================================
-- 1. SEND LEDGER
-- =============================================================================

CREATE TABLE IF NOT EXISTS send_ledger (
    id BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
    message_id VARCHAR(255) NOT NULL COMMENT 'Unique message ID (same key as deliveries and delivery_events)',
    rcpt VARCHAR(255) NOT NULL COMMENT 'Recipient address',
    domain_name VARCHAR(255) NOT NULL COMMENT 'Sender domain',
    domain_id INT UNSIGNED DEFAULT NULL COMMENT 'Domain ID (virtual_domains)',
    customer_id VARCHAR(64) DEFAULT NULL COMMENT 'client_id of the customer that owns the send',
    queue_id VARCHAR(100) DEFAULT NULL COMMENT 'Optional reference to the queued payload, for correlation',
    idempotency_key VARCHAR(255) DEFAULT NULL COMMENT 'Idempotency-Key of the /send request that admitted this send',

    -- Ledger state (SENDING-SIDE lifecycle, distinct from deliveries.status):
    --   queued        admitted by /send, waiting for the worker
    --   sending       claimed by a worker (atomic claim), SMTP in progress
    --   smtp_accepted the relay accepted it (2xx); do NOT resend; the final disposition lives in deliveries
    --   failed        permanent failure (5xx, invalid payload); terminal
    --   deadletter    max_attempts exhausted on temporary failures; terminal, needs an operator
    --   (051_send_ledger_delivered_reaper.sql adds delivered and deferred)
    state ENUM('queued','sending','smtp_accepted','failed','deadletter')
        NOT NULL DEFAULT 'queued'
        COMMENT 'Send state in the ledger (source of truth on the sending side)',

    attempt INT NOT NULL DEFAULT 0 COMMENT 'Send attempts made so far',
    max_attempts INT NOT NULL DEFAULT 3 COMMENT 'Maximum attempts before deadletter',

    worker_id VARCHAR(100) DEFAULT NULL COMMENT 'ID of the worker that holds the current claim',
    lease_expires_at TIMESTAMP NULL DEFAULT NULL COMMENT 'When the claim lease expires; after that the reaper can claim the row again',

    last_error VARCHAR(1000) DEFAULT NULL COMMENT 'Last error message (deferral or failure)',
    last_smtp_code SMALLINT DEFAULT NULL COMMENT 'Last SMTP code seen',

    admitted_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT 'When /send admitted the row',
    claimed_at TIMESTAMP NULL DEFAULT NULL COMMENT 'When a worker last claimed the row',
    terminal_at TIMESTAMP NULL DEFAULT NULL COMMENT 'When the row reached a terminal state',
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,

    -- One row per recipient: a single admission row per (message_id, rcpt), which is
    -- what makes the atomic claim (a conditional UPDATE) safe against double sending.
    UNIQUE KEY uniq_message_rcpt (message_id, rcpt),
    INDEX idx_state (state),
    -- Reaper: sweeps expired claims (state='sending' AND lease_expires_at < NOW()).
    INDEX idx_state_lease (state, lease_expires_at),
    INDEX idx_domain_state (domain_name, state),
    INDEX idx_customer (customer_id),
    INDEX idx_admitted_at (admitted_at),
    INDEX idx_queue_id (queue_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
COMMENT='Durable send ledger: admission -> atomic pre-SMTP claim -> terminal state (at-least-once + dedupe)';

-- =============================================================================
-- Atomic claim (reference: used by the worker, NOT executed here):
--
--   UPDATE send_ledger
--      SET state='sending', worker_id=:wid, attempt=attempt+1,
--          claimed_at=NOW(), lease_expires_at=NOW() + INTERVAL :lease_sec SECOND
--    WHERE message_id=:mid AND rcpt=:rcpt
--      AND (state='queued' OR (state='sending' AND lease_expires_at < NOW()));
--
-- rowcount=1 -> claim won (this worker sends). rowcount=0 -> another worker holds
-- the claim or the row is already smtp_accepted/terminal -> SKIP (pre-SMTP dedupe).
-- After the relay accepts: UPDATE ... SET state='smtp_accepted', terminal_at=NOW().
-- =============================================================================
