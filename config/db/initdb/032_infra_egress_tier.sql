-- Egress tier controller (automatic upgrade/downgrade, shared <-> dedicated node).
--
-- The operator DECLARES the plan (zone -> designated dedicated relay); the controller
-- (services/infra/scripts/egress_tier_controller.py, the hourly tier_controller job)
-- DECIDES from the volume observed in delivery_events and ACTS on zone_relays:
--   upgrade   daily average (7-day window) >= up_daily    -> bind zone -> dedicated relay
--   downgrade daily average (14-day window) < down_daily -> remove the bind (the zone goes
--             back to the shared node through a suffix bind or the default relay)
-- Cooldown: min_days_between_moves between any two decisions (avoids flapping).
-- mode='manual' = the controller observes and logs, it never acts on the zone.

CREATE TABLE IF NOT EXISTS egress_tier_plan (
  zone_domain            VARCHAR(255) PRIMARY KEY,
  dedicated_relay_id     INT          NOT NULL,      -- relays.id of the designated dedicated node
  mode                   ENUM('auto','manual') NOT NULL DEFAULT 'auto',
  up_daily               INT          NOT NULL DEFAULT 3000,
  down_daily             INT          NOT NULL DEFAULT 500,
  min_days_between_moves INT          NOT NULL DEFAULT 7,
  last_moved_at          TIMESTAMP    NULL,
  last_decision          VARCHAR(32)  NULL,          -- upgrade|downgrade|hold|dry_run:*
  created_at             TIMESTAMP    NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at             TIMESTAMP    NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS egress_tier_log (
  id          BIGINT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY,
  zone_domain VARCHAR(255) NOT NULL,
  action      VARCHAR(32)  NOT NULL,  -- upgrade|downgrade|dry_run:upgrade|dry_run:downgrade|skip:*
  detail      TEXT         NULL,
  created_at  TIMESTAMP    NULL DEFAULT CURRENT_TIMESTAMP,
  KEY idx_zone_time (zone_domain, created_at)
);
