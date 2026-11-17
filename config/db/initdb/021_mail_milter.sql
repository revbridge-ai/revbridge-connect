-- =============================================================================
-- Schema: milter and events updater (services/milter)
-- =============================================================================
-- Tables for email deliveries, events, and suppressions

-- =============================================================================
-- 1. DELIVERIES
-- =============================================================================

-- Email delivery status tracking
CREATE TABLE IF NOT EXISTS deliveries (
    id BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
    message_id VARCHAR(255) NOT NULL COMMENT 'Unique message ID',
    rcpt VARCHAR(255) NOT NULL COMMENT 'Recipient address',
    domain_name VARCHAR(255) NOT NULL COMMENT 'Sender domain (taken from the From address)',
    domain_id INT UNSIGNED DEFAULT NULL COMMENT 'Domain ID in virtual_domains',
    status VARCHAR(50) NOT NULL COMMENT 'delivered, bounced, deferred, failed',
    attempt INT NOT NULL DEFAULT 1 COMMENT 'Number of delivery attempts',
    last_update TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT 'Last status update',
    first_seen TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT 'When the message was first seen',
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,

    UNIQUE KEY unique_message_rcpt (message_id, rcpt),
    INDEX idx_status (status),
    INDEX idx_last_update (last_update),
    INDEX idx_rcpt (rcpt),
    INDEX idx_domain_status (domain_name, status),
    INDEX idx_domain_last_update (domain_name, last_update),
    INDEX idx_domain_id (domain_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
COMMENT='Final delivery status per message and recipient';

-- =============================================================================
-- 2. DELIVERY EVENTS
-- =============================================================================

-- Detailed delivery event history
CREATE TABLE IF NOT EXISTS delivery_events (
    id BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
    message_id VARCHAR(255) NOT NULL COMMENT 'Message ID',
    rcpt VARCHAR(255) NOT NULL COMMENT 'Recipient address',
    domain_name VARCHAR(255) NOT NULL COMMENT 'Sender domain',
    domain_id INT UNSIGNED DEFAULT NULL COMMENT 'Domain ID in virtual_domains',
    queue_id VARCHAR(100) NOT NULL COMMENT 'Postfix queue ID',
    status VARCHAR(50) NOT NULL COMMENT 'Event status',
    dsn TEXT COMMENT 'Delivery Status Notification',
    attempt INT NOT NULL DEFAULT 1 COMMENT 'Attempt number',
    host VARCHAR(255) DEFAULT NULL COMMENT 'Host that processed the event',
    service VARCHAR(100) DEFAULT NULL COMMENT 'Service (Postfix process) that processed the event',
    timestamp TIMESTAMP NULL DEFAULT NULL COMMENT 'Time of the original event',
    ts TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT 'Insertion time',
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,

    UNIQUE KEY unique_delivery_event (message_id, rcpt, status, timestamp),
    -- no idx_message_rcpt: unique_delivery_event already covers it
    INDEX idx_queue_id (queue_id),
    INDEX idx_status (status),
    INDEX idx_ts (ts),
    INDEX idx_host_service (host, service),
    INDEX idx_domain_ts (domain_name, ts),
    INDEX idx_domain_timestamp (domain_name, timestamp),
    INDEX idx_domain_id (domain_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
COMMENT='Delivery event history';

-- =============================================================================
-- 3. SUPPRESSIONS
-- =============================================================================

-- Email suppression list (bounces, complaints, unsubscribes)
CREATE TABLE IF NOT EXISTS suppressions (
    id BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
    email VARCHAR(255) NOT NULL COMMENT 'Suppressed email address',
    domain_name VARCHAR(255) NOT NULL COMMENT 'Sender domain',
    suppression_type ENUM('bounce', 'complaint', 'unsubscribe', 'manual') NOT NULL COMMENT 'Suppression type',
    reason VARCHAR(500) DEFAULT NULL COMMENT 'Suppression reason',
    bounce_type VARCHAR(100) DEFAULT NULL COMMENT 'Bounce type',
    complaint_feedback_type VARCHAR(100) DEFAULT NULL COMMENT 'Complaint feedback type',
    message_id VARCHAR(255) DEFAULT NULL COMMENT 'Message that caused the suppression',
    suppressed_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    expires_at TIMESTAMP NULL DEFAULT NULL,
    is_active TINYINT(1) NOT NULL DEFAULT 1,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,

    UNIQUE KEY unique_email_domain (email, domain_name),
    INDEX idx_suppression_type (suppression_type),
    INDEX idx_suppressed_at (suppressed_at),
    INDEX idx_is_active (is_active),
    INDEX idx_bounce_type (bounce_type),
    INDEX idx_complaint_feedback_type (complaint_feedback_type),
    INDEX idx_suppressions_email_active (email, is_active),
    INDEX idx_suppressions_domain_type_active (domain_name, suppression_type, is_active),
    INDEX idx_suppressions_expires_active (expires_at, is_active)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
COMMENT='Suppressed addresses per sender domain';

-- =============================================================================
-- 4. SUPPRESSION EVENTS
-- =============================================================================

-- Suppression audit log
CREATE TABLE IF NOT EXISTS suppression_events (
    id BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
    suppression_id BIGINT UNSIGNED NOT NULL,
    email VARCHAR(255) NOT NULL,
    domain_name VARCHAR(255) NOT NULL,
    event_type ENUM('created', 'updated', 'deactivated', 'reactivated', 'expired') NOT NULL,
    previous_status TINYINT(1) DEFAULT NULL,
    new_status TINYINT(1) DEFAULT NULL,
    reason VARCHAR(500) DEFAULT NULL,
    user_id VARCHAR(100) DEFAULT NULL,
    ip_address VARCHAR(45) DEFAULT NULL,
    timestamp TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,

    INDEX idx_suppression_id (suppression_id),
    INDEX idx_email_domain (email, domain_name),
    INDEX idx_event_type (event_type),
    INDEX idx_timestamp (timestamp),
    INDEX idx_user_id (user_id),
    INDEX idx_suppression_events_email_timestamp (email, timestamp),
    FOREIGN KEY (suppression_id) REFERENCES suppressions(id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
COMMENT='Suppression audit log';
