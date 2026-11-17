-- Warm-up state PER IP of the sending nodes.
--
-- A cold IP has a low reputation and mailbox providers block it (Gmail: 5.7.1 "very low
-- reputation"), so real mail cannot be poured into a cold IP. Each IP gets a CONTROLLED,
-- growing daily volume (services/infra/scripts/ip_warmup_controller.py, the hourly
-- ip_warmup job). The split (a share of the real mail -> the warming IP, the rest -> its
-- normal route) is decided in the milter by recipient hash + the daily cap kept here.
--
-- States:
--   cold    -> not started yet (cap=0)
--   warming -> receiving the daily cap, ramps on clean days
--   warm    -> the cap reached full volume, the IP joins the regular rotation
--   paused  -> turned sour (high bounce/block rate or a DNSBL listing) -> the cap goes down or to zero

CREATE TABLE IF NOT EXISTS ip_warmup (
  ip            VARCHAR(45)  NOT NULL PRIMARY KEY,
  pool_name     VARCHAR(64)  NOT NULL,
  relay_host    VARCHAR(255) NOT NULL,   -- hostname of the IP, for the FILTER smtp:[host]:25
  state         ENUM('cold','warming','warm','paused') NOT NULL DEFAULT 'cold',
  daily_cap     INT          NOT NULL DEFAULT 0,     -- current daily target volume (curve)
  consecutive_clean INT      NOT NULL DEFAULT 0,     -- consecutive clean days (ramp)
  last_bounce_rate DECIMAL(5,2) NULL,
  last_block_rate  DECIMAL(5,2) NULL,
  last_sent     INT          NULL,
  last_decision VARCHAR(48)  NULL,
  updated_at    TIMESTAMP    NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS ip_warmup_log (
  id          BIGINT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY,
  ip          VARCHAR(45)  NOT NULL,
  action      VARCHAR(48)  NOT NULL,  -- ramp|hold|pause|resume|promote
  detail      TEXT         NULL,
  created_at  TIMESTAMP    NULL DEFAULT CURRENT_TIMESTAMP,
  KEY idx_ip_time (ip, created_at)
);
