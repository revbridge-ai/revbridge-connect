-- Egress routing by RECIPIENT DOMAIN (recipient-based).
--
-- Complements relays/zone_relays (routing by SENDER): here the next hop is chosen by
-- the recipient domain. Postfix reads it through transport_maps (mysql:), which TAKES
-- PRECEDENCE over sender_dependent_relayhost_maps.
--
-- Main use: a mailbox provider that rejects the IPs of one node is routed through a
-- node or relay that it accepts, and everything else goes straight to the MX. The
-- recipient_failover and node_health jobs (services/infra/scripts) maintain the rows,
-- and an operator can add rows by hand.
--
-- Example: Microsoft consumer mailboxes (hotmail.com, outlook.com, live.com, msn.com)
-- reject mail from IPs without a sending history (S3140), while Microsoft 365 business
-- domains (*.mail.protection.outlook.com) accept them. Only the consumer domains need
-- an entry.
--
-- Changing the target is one UPDATE.

CREATE TABLE IF NOT EXISTS recipient_relays (
  recipient_domain VARCHAR(255) NOT NULL PRIMARY KEY,  -- recipient domain, e.g. outlook.com
  relay_id         INT          NOT NULL,              -- relays.id of the target relay
  notes            VARCHAR(255) NULL,
  is_active        TINYINT(1)   NOT NULL DEFAULT 1,
  created_at       TIMESTAMP    NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at       TIMESTAMP    NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- Example (not run): the Microsoft consumer domains -> a relay named 'trusted-relay'.
-- INSERT INTO recipient_relays (recipient_domain, relay_id) SELECT d, (SELECT id FROM relays WHERE name='trusted-relay')
--   FROM (SELECT 'hotmail.com' d UNION SELECT 'outlook.com' UNION SELECT 'live.com'
--         UNION SELECT 'msn.com' UNION SELECT 'hotmail.com.br' UNION SELECT 'outlook.com.br'
--         UNION SELECT 'live.com.br' UNION SELECT 'msn.com.br' UNION SELECT 'passport.com'
--         UNION SELECT 'windowslive.com' UNION SELECT 'hotmail.co.uk' UNION SELECT 'outlook.es') t;
-- Changing the target: UPDATE recipient_relays SET relay_id=(SELECT id FROM relays WHERE name='another-relay');
