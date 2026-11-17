-- Relay routing (egress) tables. One relay = one sending node (or an external relay).
-- Postfix reads the SAME tables through mysql: maps (sender_dependent_relayhost_maps,
-- smtp_sasl_password_maps, smtp_tls_policy_maps, see mta/entrypoint.sh), so a change
-- takes effect within the Postfix lookup cache TTL (minutes), with no reload or redeploy.

CREATE TABLE IF NOT EXISTS relays (
  id            INT AUTO_INCREMENT PRIMARY KEY,
  name          VARCHAR(64)  NOT NULL UNIQUE,
  smtp_host     VARCHAR(255) NOT NULL,
  smtp_port     INT          NOT NULL DEFAULT 587,
  sasl_user     VARCHAR(255) NULL,           -- NULL/'' => unauthenticated (e.g. IP-allowlisted)
  sasl_password VARCHAR(255) NULL,           -- password for sasl_user
  tls_level     VARCHAR(16)  NOT NULL DEFAULT 'encrypt',  -- may | encrypt | none
  is_default    TINYINT(1)   NOT NULL DEFAULT 0,          -- the global fallback relay (single)
  created_at    TIMESTAMP    NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at    TIMESTAMP    NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS zone_relays (
  zone_domain VARCHAR(255) PRIMARY KEY,      -- e.g. example.com (suffix match on the envelope sender)
  relay_id    INT NOT NULL,
  created_at  TIMESTAMP NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at  TIMESTAMP NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  CONSTRAINT fk_zone_relays_relay FOREIGN KEY (relay_id) REFERENCES relays (id)
);
