-- State of the adaptive warm-up controller (closed loop per sending node).
--
-- The controller (services/infra/scripts/warmup_controller.py, the warmup_controller
-- job) measures REAL delivery signals per node in delivery_events (bounce % and
-- 5.7.x block %) and decides:
--   GREEN    (low signals)              -> raises the daily budget along the curve
--   YELLOW   (rising)                   -> holds the budget
--   RED      (high bounce/block rate)   -> BRAKES: moves the zones of the node to another
--                                           route (zone_relays) and zeroes the budget
--   RECOVERING (braked + N green cycles) -> restores: the zones come back to the node,
--                                           re-warming from a low step.
--
-- Reference curve: starts at ~200/day, +20-50%/day, ~30-41 days to full volume,
-- always watching bounces and blocks.

CREATE TABLE IF NOT EXISTS warmup_state (
  pool_relay_id     INT          NOT NULL PRIMARY KEY,  -- relays.id of the node
  pool_name         VARCHAR(64)  NOT NULL,
  state             ENUM('warming','holding','braked','overflow') NOT NULL DEFAULT 'warming',
  daily_budget      INT          NOT NULL DEFAULT 200,  -- current daily target (curve)
  consecutive_green INT          NOT NULL DEFAULT 0,
  last_bounce_rate  DECIMAL(5,2) NULL,
  last_block_rate   DECIMAL(5,2) NULL,     -- % of 5.7.x (block/reputation)
  last_sent         INT          NULL,
  last_decision     VARCHAR(48)  NULL,
  braked_at         TIMESTAMP    NULL,
  last_moved_at     TIMESTAMP    NULL,
  updated_at        TIMESTAMP    NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS warmup_log (
  id          BIGINT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY,
  pool_name   VARCHAR(64)  NOT NULL,
  action      VARCHAR(48)  NOT NULL,  -- ramp|hold|brake|restore|dry_run:*
  detail      TEXT         NULL,
  created_at  TIMESTAMP    NULL DEFAULT CURRENT_TIMESTAMP,
  KEY idx_pool_time (pool_name, created_at)
);
