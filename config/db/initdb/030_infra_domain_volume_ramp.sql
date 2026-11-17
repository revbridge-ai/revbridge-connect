-- Daily volume quota PER sending DOMAIN (ramp-up governor).
--
-- Reputation lives on the sending_domain: a sudden jump in volume (50k -> 200k in a day)
-- means throttling/spam at the mailbox providers. This table keeps the quota of the day
-- per domain, moved by the volume ramp controller (services/infra/scripts/volume_ramp_controller.py:
-- 1.3x per clean day, capped at 2x the real volume, a brake on bounces/blocks, decay on
-- inactivity, cold after 30 idle days).
--
-- POST /send enforces the quota (mail_api.services.volume_governor, VOLUME_GOVERNOR_MODE).
-- See docs/deliverability.md.
--
-- Key = send_ledger.domain_name (the same key space as /send, where the gate acts).
-- Health is measured in delivery_events, matching the domain + rbm<id>.<zone>.
--
-- States:
--   active -> governed, the quota follows the curve (ramp/hold/decay)
--   braked -> red health, the quota was halved; it resumes when the window is clean
--   cold   -> >30 days without sending, the quota goes back to the bottom of the ladder (re-warm)

CREATE TABLE IF NOT EXISTS domain_volume_ramp (
  domain_name      VARCHAR(255) NOT NULL PRIMARY KEY,
  state            ENUM('active','braked','cold') NOT NULL DEFAULT 'active',
  exempt           TINYINT(1) NOT NULL DEFAULT 0 COMMENT 'Exempt from ENFORCEMENT (purely transactional). Curve/health are still observed; the /send gate skips exempt=1',
  daily_quota      INT UNSIGNED NOT NULL DEFAULT 0 COMMENT 'Current daily send quota (curve)',
  baseline_peak    INT UNSIGNED NULL COMMENT 'Peak daily volume in the lookback window when the row was created (VR_INIT_LOOKBACK_D)',
  last_active_date DATE NULL COMMENT 'Last day with a /send admission (basis for idle and decay)',
  last_admitted_24h INT UNSIGNED NULL,
  last_bounce_rate DECIMAL(5,2) NULL,
  last_block_rate  DECIMAL(5,2) NULL,
  last_decision    VARCHAR(48)  NULL,
  created_at       TIMESTAMP    NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at       TIMESTAMP    NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS domain_volume_ramp_log (
  id          BIGINT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY,
  domain_name VARCHAR(255) NOT NULL,
  action      VARCHAR(48)  NOT NULL,  -- init|ramp|hold|brake|resume|decay|cool|rewarm
  detail      TEXT         NULL,
  created_at  TIMESTAMP    NULL DEFAULT CURRENT_TIMESTAMP,
  KEY idx_domain_time (domain_name, created_at)
);
