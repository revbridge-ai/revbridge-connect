-- RevBridge Connect: subject and sender of each message, for the email list of the panel.
-- Written by POST /send (best-effort).
CREATE TABLE IF NOT EXISTS message_meta (
    message_id VARCHAR(255) NOT NULL PRIMARY KEY,
    customer_id VARCHAR(64) DEFAULT NULL COMMENT 'client_id of the tenant (same as send_ledger.customer_id)',
    sender_email VARCHAR(255) DEFAULT NULL,
    subject VARCHAR(998) DEFAULT NULL,
    recipients_count INT UNSIGNED NOT NULL DEFAULT 1,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    KEY idx_message_meta_customer_created (customer_id, created_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
