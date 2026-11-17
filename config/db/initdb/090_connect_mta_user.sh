#!/bin/sh
# RevBridge Connect — read-only database user for the MTA.
#
# The MTA (Postfix, Dovecot, OpenDKIM) is the internet-facing service on ports 25/587/465. It only
# READS: domains and aliases (Postfix), the API key for SASL (Dovecot), the DKIM key and identity
# (OpenDKIM) and the per-node routes (relays). This user has SELECT on those tables only and, in
# api_keys, only on the columns Dovecot queries.
#
# Runs once, when the MySQL volume is created (docker-entrypoint-initdb.d). The image's entrypoint
# either executes or sources *.sh files, so everything lives in a function: no `set -e` and no
# `exit` that would leak into the entrypoint's own shell.

connect_mta_user() {
    if [ -z "${MTA_DB_PASSWORD:-}" ]; then
        echo "090_connect_mta_user: MTA_DB_PASSWORD is not set (run make env)" >&2
        return 1
    fi
    _db="${MYSQL_DATABASE}"
    _user="${MTA_DB_USER:-connect_mta}"
    mysql --protocol=socket -uroot -p"${MYSQL_ROOT_PASSWORD}" <<EOSQL || return 1
CREATE USER IF NOT EXISTS '${_user}'@'%' IDENTIFIED BY '${MTA_DB_PASSWORD}';
GRANT SELECT (api_key_id, key_hash, is_active, permissions, expires_at) ON \`${_db}\`.api_keys TO '${_user}'@'%';
GRANT SELECT ON \`${_db}\`.dkim_keys TO '${_user}'@'%';
GRANT SELECT ON \`${_db}\`.dkim_identities TO '${_user}'@'%';
GRANT SELECT ON \`${_db}\`.relays TO '${_user}'@'%';
GRANT SELECT ON \`${_db}\`.zone_relays TO '${_user}'@'%';
GRANT SELECT ON \`${_db}\`.recipient_relays TO '${_user}'@'%';
GRANT SELECT ON \`${_db}\`.virtual_domains TO '${_user}'@'%';
GRANT SELECT ON \`${_db}\`.virtual_aliases TO '${_user}'@'%';
GRANT SELECT ON \`${_db}\`.virtual_users TO '${_user}'@'%';
FLUSH PRIVILEGES;
EOSQL
    echo "090_connect_mta_user: read-only user ${_user} created"
}

connect_mta_user
