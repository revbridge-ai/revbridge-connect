-- Health of each egress IP of the sending nodes (rotation membership).
--
-- A table of its own (not shared with warmup_state), so that two controllers never
-- write the same rows.
--
-- One row per IP: whether it is in the rotation (in_poolset), its last DNSBL reading
-- (Spamhaus) and its FCrDNS check. Read by the milter warm-up loader
-- (mail_milter.warmup.loader) and by services/infra/scripts/warmup_spf_authz.py: only a
-- healthy IP (in_poolset and not listed) receives warm-up traffic.
--
-- DNSBL hysteresis: the public Spamhaus resolvers RATE-LIMIT and can answer a false
-- NXDOMAIN (looks clean) for an IP that is listed. So a "listed" reading takes the IP
-- out at once and resets clean_streak, and the IP only comes back after several
-- CONSECUTIVE clean readings. A single false NXDOMAIN is not enough to bring it back.

CREATE TABLE IF NOT EXISTS poolset_state (
  pool_relay_id     INT          NOT NULL PRIMARY KEY,  -- relays.id of the node
  pool_name         VARCHAR(64)  NOT NULL,
  ip                VARCHAR(45)  NULL,
  in_poolset        BOOLEAN      NOT NULL DEFAULT FALSE, -- in the rotation?
  dnsbl_listed      BOOLEAN      NOT NULL DEFAULT FALSE, -- last reading
  fcrdns_ok         BOOLEAN      NOT NULL DEFAULT FALSE,
  clean_streak      INT          NOT NULL DEFAULT 0,     -- consecutive clean readings
  last_action       VARCHAR(32)  NULL,                   -- add|remove|keep|keep_out
  last_sent         INT          NULL,
  listed_at         TIMESTAMP    NULL,
  updated_at        TIMESTAMP    NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
