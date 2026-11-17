-- =============================================================================
-- Schema: kafka_outbox (shared; the name is historical, no Kafka broker is involved)
-- =============================================================================
--
-- Event outbox, for at-least-once delivery of events. Every service writes its
-- events to this one table.
--
-- Flow:
-- 1. A service writes the event with status 'pending'
-- 2. The webhook dispatcher (mail_api.webhook_dispatch) claims it ('processing')
--    and delivers it to the registered webhooks
-- 3. The status becomes 'sent', or goes back to 'pending' for a retry and ends
--    as 'failed' after the last attempt

CREATE TABLE IF NOT EXISTS kafka_outbox (
    -- Identification
    id              BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,

    -- Service and event type
    service_type    ENUM('email') NOT NULL DEFAULT 'email',
    event_type      VARCHAR(50) NOT NULL COMMENT 'sent, delivered, opened, clicked, etc',
    message_id      VARCHAR(255) NOT NULL COMMENT 'ID of the message the event refers to',

    -- Email fields
    rcpt            VARCHAR(255) NULL COMMENT 'Recipient email address',
    domain_name     VARCHAR(255) NULL COMMENT 'Sending domain',

    -- Customer
    client_id       VARCHAR(255) NULL COMMENT 'Customer client_id',

    -- Event payload (normalized JSON)
    payload         JSON NOT NULL COMMENT 'Serialized UnifiedEngagementEvent',

    -- Processing status
    status          ENUM('pending', 'processing', 'sent', 'failed') NOT NULL DEFAULT 'pending',
    retry_count     INT UNSIGNED NOT NULL DEFAULT 0,
    error_message   TEXT NULL COMMENT 'Last error message',

    -- Timestamps
    created_at      TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at      TIMESTAMP NULL ON UPDATE CURRENT_TIMESTAMP,
    sent_at         TIMESTAMP NULL COMMENT 'When the webhook dispatcher finished the event (status sent)',

    -- Indexes for frequent queries
    INDEX idx_service_type (service_type),
    INDEX idx_event_type (event_type),
    INDEX idx_message_id (message_id),
    INDEX idx_created_at (created_at),
    INDEX idx_status_created (status, created_at),
    INDEX idx_status_service (status, service_type, created_at)

) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
COMMENT='Event outbox for at-least-once delivery to webhooks';
