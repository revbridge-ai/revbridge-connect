-- DKIM private key material (PEM), stored next to its on-disk path.
-- OpenDKIM looks the key up in this column when it signs (mta/keytable-inline.lua, by
-- cluster_id + selector + dkim_domain), so no key files and no per-domain secrets are needed.
-- The API writes it when a key is generated or imported (mail_api.models.dkim_models), and
-- the domain_auth_audit job compares it with the public key published in DNS.

ALTER TABLE dkim_keys ADD COLUMN private_key_pem TEXT NULL;
