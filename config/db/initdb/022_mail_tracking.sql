-- =============================================================================
-- Schema: tracking service (services/tracking)
-- =============================================================================
-- Tables for email engagement tracking (opens, clicks, unsubscribes)

-- =============================================================================
-- 1. ENGAGEMENT EVENTS
-- =============================================================================

-- Email engagement events (opens, clicks, unsubscribes)
CREATE TABLE IF NOT EXISTS engagement_events (
    id BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
    message_id VARCHAR(255) NOT NULL,
    rcpt VARCHAR(255) NOT NULL,

    -- Event type
    type ENUM('open', 'click', 'unsubscribe') NOT NULL,
    url TEXT,
    token VARCHAR(255) NOT NULL,

    -- User details
    ip_address VARCHAR(45) DEFAULT NULL,
    user_agent TEXT,
    referer TEXT,

    -- Open tracking details (Gmail/Apple MPP detection)
    open_source ENUM('gmail_proxy', 'apple_mpp', 'other_proxy', 'direct', 'unknown') DEFAULT NULL,
    open_status ENUM('proxy_only', 'likely_human', 'confirmed_human') DEFAULT NULL,
    open_confidence_score TINYINT UNSIGNED DEFAULT NULL,

    -- Bot detection (mail-client prefetch, security scanners, headless, cloud
    -- VPS, Apple MPP by IP). Bot rows are persisted for audit but excluded
    -- from the outbox events and from open promotion. See
    -- services/tracking/.../services/bot_detection.py for rules.
    is_bot BOOLEAN NOT NULL DEFAULT FALSE,
    bot_reason VARCHAR(64),

    -- Domain
    domain_id INT UNSIGNED NOT NULL,

    -- Timestamps
    ts TIMESTAMP NULL DEFAULT CURRENT_TIMESTAMP,
    created_at TIMESTAMP NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,

    INDEX idx_rcpt (rcpt),
    INDEX idx_token (token),
    INDEX idx_ts (ts),
    INDEX idx_message_rcpt (message_id, rcpt),
    INDEX idx_type_status (type, open_status),
    INDEX idx_open_source (open_source),
    INDEX idx_open_score (open_confidence_score),
    INDEX idx_is_bot (is_bot),
    INDEX idx_domain_id (domain_id)
    -- FK (not declared): FOREIGN KEY (domain_id) REFERENCES virtual_domains(id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
