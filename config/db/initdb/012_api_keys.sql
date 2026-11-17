-- =====================================================
-- Schema: API keys and channels (shared)
-- =====================================================
--
-- STRUCTURE:
--
-- Customer (customers)
--     └── API key (api_keys) via client_id
--             └── Enabled channels (api_key_channels)
--                     └── Email (config: domain_id, include_subdomains)
--
-- An API key is bound to a client_id, its main identifier.
-- client_id references customers.client_id.
-- Each channel has its own settings (e.g. domain_id for email).
--
-- The customers table is defined in 010_mail_infra.sql.
-- =====================================================

-- =====================================================
-- 1. COMMUNICATION CHANNELS
-- =====================================================
CREATE TABLE IF NOT EXISTS channels (
    id INT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
    name VARCHAR(50) NOT NULL UNIQUE COMMENT 'Channel name (Connect: email)',
    display_name VARCHAR(100) NOT NULL COMMENT 'Display name of the channel',
    description VARCHAR(255) NULL COMMENT 'Channel description',
    is_active BOOLEAN DEFAULT TRUE COMMENT 'Whether the channel is available',
    config_schema JSON NULL COMMENT 'JSON Schema expected for the channel config',

    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,

    INDEX idx_is_active (is_active)
) ENGINE=InnoDB
  DEFAULT CHARSET=utf8mb4
  COLLATE=utf8mb4_unicode_ci
  COMMENT='Available communication channels';

-- =====================================================
-- 2. API KEYS
-- =====================================================
CREATE TABLE IF NOT EXISTS api_keys (
    id INT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
    api_key_id VARCHAR(255) NOT NULL UNIQUE,
    name VARCHAR(255) NOT NULL,

    -- Secure hash of the key (SHA512-CRYPT)
    key_hash VARCHAR(255) NOT NULL UNIQUE,

    -- Fast hash for an O(1) lookup (SHA256)
    -- Filled in automatically on creation or on the first validation
    key_lookup_hash VARCHAR(64) NULL COMMENT 'SHA256 hash for O(1) lookup',

    -- Owner: either user_id OR service_account_id
    user_id VARCHAR(255) NULL,
    service_account_id VARCHAR(255) NULL,

    -- Client ID: the main identifier of the customer (UUID)
    -- Every API key must have a client_id
    client_id VARCHAR(36) NOT NULL COMMENT 'UUID of the customer that owns the API key (customers.client_id)',

    -- domain_id kept for compatibility (deprecated)
    -- New code uses api_key_channels.config.domain_id
    domain_id INT UNSIGNED NULL COMMENT 'DEPRECATED: use api_key_channels for email',

    permissions JSON NOT NULL,
    is_active BOOLEAN DEFAULT TRUE,
    last_used_at TIMESTAMP NULL,
    expires_at TIMESTAMP NULL,

    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,

    -- Foreign keys (not declared, to avoid a circular dependency between the init scripts):
    -- FOREIGN KEY (user_id) REFERENCES users(user_id) ON DELETE CASCADE,
    -- FOREIGN KEY (domain_id) REFERENCES virtual_domains(id) ON DELETE SET NULL,
    -- FOREIGN KEY (service_account_id) REFERENCES service_accounts(service_account_id) ON DELETE CASCADE,

    -- Indexes
    INDEX idx_user_id (user_id),
    INDEX idx_domain_id (domain_id),
    INDEX idx_client_id (client_id),
    INDEX idx_is_active (is_active),
    INDEX idx_expires_at (expires_at),
    INDEX idx_service_account_id (service_account_id),
    INDEX idx_key_lookup_hash (key_lookup_hash),
    INDEX idx_lookup_active_expires (key_lookup_hash, is_active, expires_at),

    -- user_id XOR service_account_id
    CHECK (
        (user_id IS NOT NULL AND service_account_id IS NULL)
        OR
        (user_id IS NULL AND service_account_id IS NOT NULL)
    )
) ENGINE=InnoDB
  DEFAULT CHARSET=utf8mb4
  COLLATE=utf8mb4_unicode_ci;

-- =====================================================
-- 3. API KEY <-> CHANNEL RELATION
-- =====================================================
CREATE TABLE IF NOT EXISTS api_key_channels (
    id INT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
    api_key_id INT UNSIGNED NOT NULL COMMENT 'FK to api_keys.id',
    channel_id INT UNSIGNED NOT NULL COMMENT 'FK to channels.id',
    is_active BOOLEAN DEFAULT TRUE COMMENT 'Whether the channel is enabled for this API key',

    -- Channel settings for this API key
    -- Email: {"domain_id": 123, "include_subdomains": true}
    config JSON NULL COMMENT 'Channel-specific settings',

    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,

    FOREIGN KEY (api_key_id) REFERENCES api_keys(id) ON DELETE CASCADE,
    FOREIGN KEY (channel_id) REFERENCES channels(id) ON DELETE RESTRICT,

    UNIQUE KEY unique_api_key_channel (api_key_id, channel_id),
    INDEX idx_channel_id (channel_id),
    INDEX idx_is_active (is_active)
) ENGINE=InnoDB
  DEFAULT CHARSET=utf8mb4
  COLLATE=utf8mb4_unicode_ci
  COMMENT='Channels enabled per API key';

-- =====================================================
-- 3b. DOMAINS AUTHORIZED PER API KEY (multi-domain)
-- =====================================================
-- Lets ONE api_key send from SEVERAL domains of the same customer.
-- Complements the primary domain (api_key_channels.config.domain_id): the
-- sending authorization (validate_authorized_sender) accepts the primary domain
-- OR any domain listed here (subdomains included). Without rows here, only the
-- primary domain is accepted.
CREATE TABLE IF NOT EXISTS api_key_domains (
    id INT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
    api_key_id INT UNSIGNED NOT NULL COMMENT 'FK to api_keys.id',
    domain_id INT UNSIGNED NOT NULL COMMENT 'FK to virtual_domains.id',
    include_subdomains BOOLEAN DEFAULT TRUE COMMENT 'Also authorize the subdomains of the domain',

    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,

    FOREIGN KEY (api_key_id) REFERENCES api_keys(id) ON DELETE CASCADE,
    FOREIGN KEY (domain_id) REFERENCES virtual_domains(id) ON DELETE CASCADE,

    UNIQUE KEY unique_api_key_domain (api_key_id, domain_id),
    INDEX idx_domain_id (domain_id)
) ENGINE=InnoDB
  DEFAULT CHARSET=utf8mb4
  COLLATE=utf8mb4_unicode_ci
  COMMENT='Additional domains authorized per API key (several domains per customer)';

-- =====================================================
-- 4. SEED DATA - DEFAULT CHANNELS
-- =====================================================
INSERT INTO channels (name, display_name, description, config_schema) VALUES
('email', 'Email', 'Transactional and marketing email', JSON_OBJECT(
    'type', 'object',
    'properties', JSON_OBJECT(
        'domain_id', JSON_OBJECT('type', 'integer', 'description', 'Virtual domain ID'),
        'include_subdomains', JSON_OBJECT('type', 'boolean', 'default', true)
    ),
    'required', JSON_ARRAY('domain_id')
))
ON DUPLICATE KEY UPDATE
    display_name = VALUES(display_name),
    description = VALUES(description),
    config_schema = VALUES(config_schema);
