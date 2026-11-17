-- =============================================================================
-- Schema: mail API (services/api)
-- =============================================================================
-- Tables for email configuration, webhooks, and approved senders

-- =============================================================================
-- 1. EMAIL CONFIGURATION
-- =============================================================================

-- Email configurations per domain/customer
CREATE TABLE IF NOT EXISTS email_configs (
    id INT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
    config_id VARCHAR(255) NOT NULL UNIQUE,
    domain_id INT UNSIGNED NOT NULL,
    name VARCHAR(255) NOT NULL COMMENT 'Descriptive name of the configuration',

    -- SMTP Configuration
    smtp_host VARCHAR(255) NOT NULL COMMENT 'SMTP server host',
    smtp_port INT UNSIGNED NOT NULL DEFAULT 587 COMMENT 'SMTP port (587, 465, 25)',
    smtp_secure TINYINT(1) DEFAULT 0 COMMENT 'Use SSL/TLS (true for port 465)',
    smtp_user VARCHAR(255) DEFAULT NULL COMMENT 'SMTP username (optional; can also be set per sender in smtp_credentials)',
    smtp_password VARCHAR(255) DEFAULT NULL COMMENT 'SMTP password',
    smtp_base_port INT UNSIGNED DEFAULT 587 COMMENT 'Base port for the dynamic port calculation',
    smtp_timeout INT UNSIGNED DEFAULT 30 COMMENT 'SMTP connection timeout in seconds',
    envelope_prefix VARCHAR(64) DEFAULT NULL COMMENT 'rbm<n> label of the SPF-aligned Return-Path domain (rbm<n>.<domain>); keeps the sender identity independent of smtp_host, so domains can share one smtp_host',

    -- Tracking Configuration
    tracking_enabled TINYINT(1) DEFAULT 1 COMMENT 'Enable open and click tracking',
    inject_pixel TINYINT(1) DEFAULT 1 COMMENT 'Inject the tracking pixel into emails',
    rewrite_links TINYINT(1) DEFAULT 1 COMMENT 'Rewrite links for click tracking',
    pixel_tracking_url VARCHAR(500) DEFAULT NULL COMMENT 'Base URL of the tracking pixel (optional; the default is used when NULL)',
    click_tracking_url VARCHAR(500) DEFAULT NULL COMMENT 'Base URL for click tracking (optional; the default is used when NULL)',
    tracking_domain VARCHAR(255) DEFAULT NULL COMMENT 'Tracking domain (e.g. tracking.example.com)',

    -- From/Reply Configuration
    default_from_name VARCHAR(255) DEFAULT NULL COMMENT 'Default sender name',
    default_from_email VARCHAR(255) DEFAULT NULL COMMENT 'Default sender email address',
    default_reply_to VARCHAR(255) DEFAULT NULL COMMENT 'Default reply-to address',

    -- Bounce/Complaint Configuration
    bounce_email VARCHAR(255) DEFAULT NULL COMMENT 'Address that receives bounces',
    complaint_email VARCHAR(255) DEFAULT NULL COMMENT 'Address that receives complaints',
    shadow_email VARCHAR(255) DEFAULT NULL COMMENT 'Address that receives a blind copy for auditing',

    -- Rate Limiting
    rate_limit_per_minute INT UNSIGNED DEFAULT 100 COMMENT 'Email limit per minute',
    rate_limit_per_hour INT UNSIGNED DEFAULT 1000 COMMENT 'Email limit per hour',
    rate_limit_per_day INT UNSIGNED DEFAULT 10000 COMMENT 'Email limit per day',

    -- Retry Configuration
    max_retries INT UNSIGNED DEFAULT 3 COMMENT 'Maximum number of send attempts',
    retry_delay_seconds INT UNSIGNED DEFAULT 60 COMMENT 'Delay between attempts, in seconds',

    -- Default Webhook
    default_webhook_url VARCHAR(500) DEFAULT NULL COMMENT 'Default webhook URL for events',
    default_webhook_secret VARCHAR(255) DEFAULT NULL COMMENT 'Default webhook secret key',

    -- Additional Configuration
    additional_config JSON DEFAULT NULL COMMENT 'Additional settings as JSON',

    -- Status & Metadata
    is_active TINYINT(1) DEFAULT 1 COMMENT 'Whether the configuration is active',
    is_default TINYINT(1) DEFAULT 0 COMMENT 'Default configuration of the domain',
    priority INT UNSIGNED DEFAULT 0 COMMENT 'Priority (higher wins)',
    notes TEXT COMMENT 'Notes about the configuration',
    created_by VARCHAR(255) DEFAULT NULL COMMENT 'User who created the configuration',
    created_at TIMESTAMP NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,

    -- Generated column that allows only ONE active default per domain
    -- NULL when the row is not the active default (NULLs do not violate the unique constraint)
    active_default_domain INT UNSIGNED GENERATED ALWAYS AS (
        CASE WHEN is_default = TRUE AND is_active = TRUE THEN domain_id ELSE NULL END
    ) STORED COMMENT 'Generated column behind the unique key on the active default',

    INDEX idx_is_active (is_active),
    INDEX idx_is_default (is_default),
    INDEX idx_priority (priority),
    INDEX idx_created_by (created_by),
    UNIQUE KEY idx_unique_active_default (active_default_domain)
    -- FK (not declared): FOREIGN KEY (domain_id) REFERENCES virtual_domains(id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
COMMENT='Email, SMTP and tracking settings per customer domain';

-- Per-sender SMTP credentials override
CREATE TABLE IF NOT EXISTS smtp_credentials (
    id INT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
    credential_id VARCHAR(255) NOT NULL UNIQUE,
    email_config_id INT UNSIGNED NOT NULL COMMENT 'Email configuration these credentials belong to (email_configs.id)',
    from_email VARCHAR(255) NOT NULL COMMENT 'Sender address that uses these credentials',
    smtp_user VARCHAR(255) NOT NULL COMMENT 'SMTP username',
    smtp_password VARCHAR(255) NOT NULL COMMENT 'SMTP password',
    is_active TINYINT(1) DEFAULT 1,
    priority INT UNSIGNED DEFAULT 0 COMMENT 'Priority (higher wins)',
    created_at TIMESTAMP NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,

    UNIQUE KEY unique_config_email (email_config_id, from_email),
    INDEX idx_from_email (from_email),
    INDEX idx_is_active (is_active),
    INDEX idx_priority (priority),
    FOREIGN KEY (email_config_id) REFERENCES email_configs(id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
COMMENT='SMTP credentials per sender address';

-- Email configuration change history (audit log)
CREATE TABLE IF NOT EXISTS email_config_history (
    id BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
    email_config_id INT UNSIGNED NOT NULL,
    action ENUM('created', 'updated', 'deleted', 'activated', 'deactivated') NOT NULL,
    changed_by VARCHAR(255) DEFAULT NULL,
    old_values JSON DEFAULT NULL COMMENT 'Previous values (snapshot)',
    new_values JSON DEFAULT NULL COMMENT 'New values (snapshot)',
    notes TEXT,
    created_at TIMESTAMP NULL DEFAULT CURRENT_TIMESTAMP,

    INDEX idx_email_config_id (email_config_id),
    INDEX idx_action (action),
    INDEX idx_changed_by (changed_by),
    INDEX idx_created_at (created_at),
    FOREIGN KEY (email_config_id) REFERENCES email_configs(id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
COMMENT='Change history of the email configurations';

-- =============================================================================
-- 2. WEBHOOKS
-- =============================================================================

-- Webhook registrations
CREATE TABLE IF NOT EXISTS webhooks (
    id INT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
    service_type ENUM('email') NOT NULL DEFAULT 'email',
    webhook_id VARCHAR(255) NOT NULL UNIQUE,
    name VARCHAR(255) NOT NULL,
    url VARCHAR(2048) NOT NULL,
    secret_key VARCHAR(255) DEFAULT NULL,
    events JSON NOT NULL,

    -- Domain & Client
    domain_name VARCHAR(255) DEFAULT NULL,
    domain_id INT UNSIGNED DEFAULT NULL,
    client_id VARCHAR(255) DEFAULT NULL,

    -- Settings
    is_active TINYINT(1) DEFAULT 1,
    retry_count INT DEFAULT 3,
    timeout_seconds INT DEFAULT 30,
    headers JSON DEFAULT NULL,

    -- Metadata
    created_by VARCHAR(255) DEFAULT NULL,
    created_at TIMESTAMP NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,

    INDEX idx_domain_name (domain_name),
    INDEX idx_is_active (is_active),
    INDEX idx_created_by (created_by),
    INDEX idx_domain_id (domain_id),
    INDEX idx_service_type (service_type),
    INDEX idx_client_id (client_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- Webhook event records
CREATE TABLE IF NOT EXISTS webhook_events (
    id BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
    service_type ENUM('email') NOT NULL DEFAULT 'email',
    webhook_id INT UNSIGNED NOT NULL,
    event_id VARCHAR(255) NOT NULL UNIQUE,
    event_type VARCHAR(50) NOT NULL,
    message_id VARCHAR(255) NOT NULL,

    -- Email fields
    recipient_email VARCHAR(255) DEFAULT NULL,
    domain_name VARCHAR(255) DEFAULT NULL,
    domain_id INT UNSIGNED DEFAULT NULL,
    client_id VARCHAR(255) DEFAULT NULL,

    -- Payload & Status
    payload JSON NOT NULL,
    status ENUM('pending', 'processing', 'success', 'delivered', 'failed', 'retrying', 'exhausted') DEFAULT 'pending',
    retry_count INT DEFAULT 0,
    max_retries INT DEFAULT 3,
    response_code INT DEFAULT NULL,
    response_body TEXT,
    error_message TEXT,
    next_retry_at TIMESTAMP NULL DEFAULT NULL,
    sent_at TIMESTAMP NULL DEFAULT NULL,
    delivered_at TIMESTAMP NULL DEFAULT NULL,
    created_at TIMESTAMP NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,

    INDEX idx_webhook_id (webhook_id),
    INDEX idx_event_type (event_type),
    INDEX idx_message_id (message_id),
    INDEX idx_recipient_email (recipient_email),
    INDEX idx_domain_name (domain_name),
    INDEX idx_status (status),
    INDEX idx_response_code (response_code),
    INDEX idx_next_retry_at (next_retry_at),
    INDEX idx_sent_at (sent_at),
    INDEX idx_created_at (created_at),
    INDEX idx_domain_id (domain_id),
    INDEX idx_service_type (service_type),
    INDEX idx_client_id (client_id),
    FOREIGN KEY (webhook_id) REFERENCES webhooks(id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- Webhook delivery attempt logs
CREATE TABLE IF NOT EXISTS webhook_logs (
    id BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
    webhook_event_id BIGINT UNSIGNED NOT NULL,
    attempt_number INT NOT NULL,
    request_url VARCHAR(2048) NOT NULL,
    request_headers JSON DEFAULT NULL,
    request_payload JSON DEFAULT NULL,
    response_code INT DEFAULT NULL,
    response_headers JSON DEFAULT NULL,
    response_body TEXT,
    duration_ms INT DEFAULT NULL,
    error_message TEXT,
    created_at TIMESTAMP NULL DEFAULT CURRENT_TIMESTAMP,

    INDEX idx_webhook_event_id (webhook_event_id),
    INDEX idx_attempt_number (attempt_number),
    INDEX idx_created_at (created_at),
    FOREIGN KEY (webhook_event_id) REFERENCES webhook_events(id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- =============================================================================
-- 3. APPROVED SENDERS
-- =============================================================================

-- Approved sender addresses per domain
CREATE TABLE IF NOT EXISTS approved_senders (
    id INT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
    sender_email VARCHAR(255) NOT NULL COMMENT 'Full sender address (e.g. news@mail.example.com)',
    principal_domain_id INT UNSIGNED NOT NULL COMMENT 'Authorized primary domain (virtual_domains.id)',
    is_active TINYINT(1) NOT NULL DEFAULT 1 COMMENT 'Whether the sender is active',
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,

    UNIQUE KEY unique_sender_email_domain (sender_email, principal_domain_id),
    INDEX idx_principal_domain_id (principal_domain_id),
    INDEX idx_is_active (is_active),
    INDEX idx_sender_principal_active (sender_email, principal_domain_id, is_active)
    -- FK (not declared): FOREIGN KEY (principal_domain_id) REFERENCES virtual_domains(id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
COMMENT='Sender addresses approved for sending';

-- =============================================================================
-- 4. DKIM KEYS
-- =============================================================================

-- DKIM keys per cluster/domain
CREATE TABLE IF NOT EXISTS dkim_keys (
    id BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
    cluster_id VARCHAR(255) NOT NULL,
    selector VARCHAR(255) NOT NULL,
    dkim_domain VARCHAR(255) NOT NULL,
    private_key_path VARCHAR(512) NOT NULL,
    created_at TIMESTAMP NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,

    UNIQUE KEY uk_cluster_selector_domain (cluster_id, selector, dkim_domain)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- DKIM signing identities
CREATE TABLE IF NOT EXISTS dkim_identities (
    id BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
    cluster_id VARCHAR(255) NOT NULL,
    from_email VARCHAR(255) DEFAULT NULL,
    from_domain VARCHAR(255) DEFAULT NULL,
    wildcard_subdomains TINYINT(1) DEFAULT 0,
    selector VARCHAR(255) NOT NULL,
    dkim_domain VARCHAR(255) NOT NULL,
    created_at TIMESTAMP NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,

    INDEX idx_cluster_domain (cluster_id, from_domain),
    INDEX idx_cluster_email (cluster_id, from_email)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
