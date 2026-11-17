-- =============================================================================
-- Schema: mail infrastructure
-- =============================================================================
-- Tables for mail infrastructure (domains, users, customers, zones)

-- =============================================================================
-- 1. VIRTUAL DOMAINS
-- =============================================================================

-- Virtual domains for mail infrastructure
CREATE TABLE IF NOT EXISTS virtual_domains (
    id INT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
    name VARCHAR(255) NOT NULL UNIQUE,
    created_at TIMESTAMP NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- =============================================================================
-- 2. VIRTUAL USERS
-- =============================================================================

-- Virtual mailbox users
CREATE TABLE IF NOT EXISTS virtual_users (
    id INT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
    domain_id INT UNSIGNED NOT NULL,
    email VARCHAR(255) NOT NULL UNIQUE,
    password VARCHAR(255) NOT NULL,
    active INT NOT NULL,
    created_at TIMESTAMP NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,

    FOREIGN KEY (domain_id) REFERENCES virtual_domains(id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- =============================================================================
-- 3. VIRTUAL ALIASES
-- =============================================================================

-- Email aliases
CREATE TABLE IF NOT EXISTS virtual_aliases (
    id INT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
    domain_id INT UNSIGNED NOT NULL,
    source VARCHAR(255) NOT NULL,
    destination VARCHAR(255) NOT NULL,
    created_at TIMESTAMP NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,

    FOREIGN KEY (domain_id) REFERENCES virtual_domains(id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- =============================================================================
-- 4. CUSTOMERS
-- =============================================================================

-- Customer records
CREATE TABLE IF NOT EXISTS customers (
    id BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
    name VARCHAR(255) NOT NULL UNIQUE,
    client_id VARCHAR(36) DEFAULT NULL UNIQUE COMMENT 'Unique customer UUID that API keys reference (api_keys.client_id)',
    contact_email VARCHAR(255) DEFAULT NULL,

    -- Status fields
    is_active TINYINT(1) DEFAULT 1 COMMENT 'Whether the customer is active',
    status ENUM('pending', 'active', 'suspended', 'cancelled') DEFAULT 'pending' COMMENT 'Customer status',
    suspended_reason VARCHAR(255) DEFAULT NULL COMMENT 'Suspension reason',
    suspended_at TIMESTAMP NULL DEFAULT NULL COMMENT 'When the customer was suspended',

    -- Channels enabled (JSON)
    channels_enabled JSON DEFAULT NULL COMMENT 'Enabled channels (["email"])',

    -- Metadata
    metadata JSON DEFAULT NULL COMMENT 'Additional metadata',
    notes TEXT COMMENT 'Internal notes',

    created_at TIMESTAMP NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,

    -- no idx_client_id: the UNIQUE KEY on client_id already covers it
    INDEX idx_status (status),
    INDEX idx_is_active (is_active)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- =============================================================================
-- 5. VM GROUPS
-- =============================================================================

-- Group of VMs (a physical/Terraform unit) with a NEUTRAL identity, decoupled from
-- the name of the customer. Several zones point to the same group (same IPs). `kind`
-- separates a shared pool (shared) from VMs dedicated to one customer (dedicated).
-- A zone without a group keeps group_id NULL.
CREATE TABLE IF NOT EXISTS vm_groups (
    id BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
    group_key VARCHAR(64) NOT NULL UNIQUE COMMENT 'Random neutral key (no counter), e.g. grp-481920',
    label VARCHAR(255) DEFAULT NULL COMMENT 'Human-readable label, e.g. Shared pool 01',
    provider VARCHAR(64) NOT NULL,
    mgmt_host VARCHAR(255) NOT NULL COMMENT 'IP of the management server (natural key of the group)',
    kind ENUM('shared','dedicated') NOT NULL DEFAULT 'shared',
    owner_client_id VARCHAR(255) DEFAULT NULL COMMENT 'client_id of the owning customer (kind=dedicated only)',
    refill_target INT NOT NULL DEFAULT 2 COMMENT 'Target number of free zones in the pool (kind=shared only)',
    status VARCHAR(32) NOT NULL DEFAULT 'active',
    created_at TIMESTAMP NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,

    KEY idx_vm_groups_mgmt (mgmt_host),
    KEY idx_vm_groups_kind (kind, status)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
  COMMENT='VM groups (neutral identity, shared or dedicated) for customer-to-group allocation';

-- =============================================================================
-- 6. ZONES
-- =============================================================================

-- Mail infrastructure zones
CREATE TABLE IF NOT EXISTS zones (
    id BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
    provider VARCHAR(64) NOT NULL,
    zone_name VARCHAR(255) NOT NULL UNIQUE,
    domain VARCHAR(255) NOT NULL,
    subdomain_lb VARCHAR(255) DEFAULT NULL,
    subdomain_customer VARCHAR(255) DEFAULT NULL,
    group_id BIGINT UNSIGNED DEFAULT NULL COMMENT 'FK to vm_groups.id (NULL when the zone has no group)',
    status VARCHAR(32) DEFAULT NULL,
    created_at TIMESTAMP NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,

    KEY idx_zones_group (group_id),
    FOREIGN KEY (group_id) REFERENCES vm_groups(id) ON DELETE SET NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- =============================================================================
-- 7. CUSTOMER ZONES
-- =============================================================================

-- Links customers to mail infrastructure zones
CREATE TABLE IF NOT EXISTS customer_zones (
    id BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
    customer_id BIGINT UNSIGNED NOT NULL,
    zone_id BIGINT UNSIGNED NOT NULL,
    status VARCHAR(32) DEFAULT 'pending',
    dns_validation_wait_until TIMESTAMP NULL DEFAULT NULL,
    linked_at TIMESTAMP NULL DEFAULT CURRENT_TIMESTAMP,

    UNIQUE KEY zone_id (zone_id, customer_id),
    FOREIGN KEY (customer_id) REFERENCES customers(id) ON DELETE CASCADE,
    FOREIGN KEY (zone_id) REFERENCES zones(id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- =============================================================================
-- 8. USERS (for auth)
-- =============================================================================

-- Users table (referenced by api_keys)
CREATE TABLE IF NOT EXISTS users (
    id INT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
    user_id VARCHAR(255) NOT NULL UNIQUE,
    username VARCHAR(255) NOT NULL UNIQUE,
    email VARCHAR(255) NOT NULL UNIQUE,
    password_hash VARCHAR(255) NOT NULL,
    domain_id INT UNSIGNED DEFAULT NULL COMMENT 'FK to virtual_domains.id',
    customer_id BIGINT UNSIGNED DEFAULT NULL COMMENT 'FK to customers.id, used to look up the client_id',
    role ENUM('admin', 'user', 'viewer') DEFAULT 'user',
    is_active TINYINT(1) DEFAULT 1,
    last_login TIMESTAMP NULL DEFAULT NULL,
    created_at TIMESTAMP NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,

    INDEX idx_domain_id (domain_id),
    INDEX idx_is_active (is_active),
    INDEX idx_customer_id (customer_id),
    FOREIGN KEY (domain_id) REFERENCES virtual_domains(id) ON DELETE SET NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- =============================================================================
-- 9. SERVICE ACCOUNTS
-- =============================================================================

-- Service accounts (for API keys without user)
CREATE TABLE IF NOT EXISTS service_accounts (
    id INT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
    service_account_id VARCHAR(255) NOT NULL UNIQUE,
    name VARCHAR(255) NOT NULL,
    description TEXT,
    domain_id INT UNSIGNED NOT NULL,
    is_active TINYINT(1) DEFAULT 1,
    last_used_at TIMESTAMP NULL DEFAULT NULL,
    created_by VARCHAR(255) DEFAULT NULL,
    created_at TIMESTAMP NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,

    INDEX created_by (created_by),
    INDEX idx_domain_id (domain_id),
    INDEX idx_is_active (is_active),
    INDEX idx_last_used_at (last_used_at),
    FOREIGN KEY (domain_id) REFERENCES virtual_domains(id) ON DELETE CASCADE,
    FOREIGN KEY (created_by) REFERENCES users(user_id) ON DELETE SET NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
