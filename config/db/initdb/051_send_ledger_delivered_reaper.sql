-- send_ledger: closing the durability loop.
--   * 'delivered'  = the REAL terminal state (the relay confirmed status=sent, through a delivery event), NOT the 250 from Postfix.
--   * 'deferred'   = the relay/Postfix deferred it, waiting for the reaper to re-inject it (the ledger IS the deferred queue).
--   * delivery_deadline = how long to wait for the delivery confirmation before re-injecting (level 2).
--   * next_attempt_at   = when the reaper should re-inject (backoff schedule, durable in the database).
--
-- 'smtp_accepted' is INTERMEDIATE (Postfix took the message) and only 'delivered' is terminal,
-- which makes a lost Postfix queue (a restart) recoverable.

ALTER TABLE send_ledger
  MODIFY COLUMN state
    ENUM('queued','sending','smtp_accepted','delivered','deferred','failed','deadletter')
    NOT NULL DEFAULT 'queued'
    COMMENT 'queued -> sending -> smtp_accepted -> delivered (terminal). deferred = waiting for the reaper. failed and deadletter are terminal.';

ALTER TABLE send_ledger
  ADD COLUMN delivery_deadline TIMESTAMP NULL DEFAULT NULL
    COMMENT 'Deadline to confirm delivery (status=sent) after smtp_accepted; once it passes without delivered, the reaper re-injects the message',
  ADD COLUMN next_attempt_at TIMESTAMP NULL DEFAULT NULL
    COMMENT 'When the reaper should re-inject the message (durable backoff schedule)',
  ADD COLUMN delivered_at TIMESTAMP NULL DEFAULT NULL
    COMMENT 'When the actual delivery was confirmed (status=sent from the relay)';

-- Indexes for the reaper sweeps (cheap, they cover exactly its queries).
ALTER TABLE send_ledger
  ADD INDEX idx_deadline (state, delivery_deadline),
  ADD INDEX idx_next_attempt (state, next_attempt_at);
