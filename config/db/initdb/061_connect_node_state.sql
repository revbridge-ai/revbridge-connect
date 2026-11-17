-- RevBridge Connect: health of each sending node (node_health_controller).
-- One node = one row in relays (the control node too, with smtp_host='direct').
CREATE TABLE IF NOT EXISTS connect_node_state (
    relay_id      INT NOT NULL PRIMARY KEY COMMENT 'relays.id of the node',
    ip            VARCHAR(45) DEFAULT NULL COMMENT 'Egress IP checked against the blocklists',
    state         ENUM('HEALTHY','LISTED','DISABLED') NOT NULL DEFAULT 'HEALTHY',
    listed_on     VARCHAR(512) DEFAULT NULL COMMENT 'Blocklists that listed the IP at the last check',
    clean_streak  INT NOT NULL DEFAULT 0 COMMENT 'Consecutive clean checks (hysteresis before the node returns)',
    zones_moved   INT NOT NULL DEFAULT 0 COMMENT 'Zones moved off the node in the last quarantine',
    last_check_at TIMESTAMP NULL DEFAULT NULL,
    changed_at    TIMESTAMP NULL DEFAULT NULL,
    updated_at    TIMESTAMP NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS connect_node_log (
    id         BIGINT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY,
    relay_id   INT NOT NULL,
    event      VARCHAR(32) NOT NULL COMMENT 'listed | released | zone_moved | recipient_moved | default_moved | no_target',
    detail     TEXT,
    dry_run    TINYINT(1) NOT NULL DEFAULT 0,
    created_at TIMESTAMP NULL DEFAULT CURRENT_TIMESTAMP,
    KEY idx_connect_node_log (relay_id, created_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
