#!/bin/bash
# RevBridge Connect — MTA boot: renders the configuration, prepares TLS, DKIM and per-node
# routing, and starts Postfix + OpenDKIM + Dovecot (SASL) + rsyslog under supervisord.
set -euo pipefail

log() { echo "[mta] $*"; }

python3 /opt/connect/render.py /opt/connect/templates

postmap /etc/postfix/transport
postmap /etc/postfix/sender_canonical 2>/dev/null || true
newaliases 2>/dev/null || true

# ─── DKIM ──────────────────────────────────────────────────────────────────
# OpenDKIM reads the private key straight from the database (dkim_keys.private_key_pem) for every
# message: a new domain signs right away, with no key file and no restart.
mkdir -p /etc/opendkim/lua /run/opendkim
cp /opt/connect/keytable-inline.lua /etc/opendkim/lua/keytable.lua
chown root:opendkim /etc/opendkim/lua/keytable.lua 2>/dev/null || true
chmod 640 /etc/opendkim/lua/keytable.lua
chown opendkim:opendkim /run/opendkim 2>/dev/null || true

postfix set-permissions >/dev/null 2>&1 || true
# no chroot: the smtp client needs the container's /etc/resolv.conf
postconf -F '*/*/chroot=n' >/dev/null 2>&1 || true

# ─── TLS ───────────────────────────────────────────────────────────────────
# Order: the certificate Caddy issued for the submission host (Let's Encrypt, caddy-data volume
# mounted at /caddy-data); otherwise the one in secrets/tls (`make env` generates a self-signed
# one for testing). supervisord's cert-watch reloads when Caddy renews it.
/opt/connect/tls-sync.sh --boot

# ─── internal queue ────────────────────────────────────────────────────────
# The queue injects into the internal submission port (QUEUE_SUBMISSION_PORT, 2587; see master.cf),
# which only exists on the compose network. There is no unauthenticated listener: every message
# comes in with the customer's API key (SASL) and goes through the milters.

# ─── per-node routing (tables relays / zone_relays / recipient_relays) ─────
# Moving a zone or a destination to another node is an UPDATE in the database: no redeploy. The
# relay with smtp_host='direct' is the control node itself, delivering straight to the MX.
_relay_cf() {  # $1=file  $2=query
    cat > "/etc/postfix/$1" <<EOF
user = ${MAILBOX_USER}
password = ${MAILBOX_PASSWORD}
hosts = ${MYSQL_HOST:-db}:${MYSQL_PORT:-3306}
dbname = ${MYSQL_DATABASE:-revbridge_connect}
query = $2
EOF
    chmod 640 "/etc/postfix/$1"
}
_relay_cf relay_by_zone.cf "SELECT CONCAT('[', r.smtp_host, ']:', r.smtp_port) FROM relays r WHERE r.id = COALESCE((SELECT zr.relay_id FROM zone_relays zr WHERE '%s' LIKE CONCAT('%%', zr.zone_domain) ORDER BY LENGTH(zr.zone_domain) DESC LIMIT 1), (SELECT id FROM relays WHERE is_default=1 LIMIT 1)) AND r.smtp_host <> 'direct' LIMIT 1"
_relay_cf relay_sasl.cf "SELECT CONCAT(r.sasl_user, ':', r.sasl_password) FROM relays r WHERE r.sasl_user IS NOT NULL AND r.sasl_user <> '' AND CONCAT('[', r.smtp_host, ']:', r.smtp_port) = '%s'"
_relay_cf relay_tls.cf "SELECT r.tls_level FROM relays r WHERE CONCAT('[', r.smtp_host, ']:', r.smtp_port) = '%s'"
# per recipient domain (e.g. a mailbox provider blocked a node): transport_maps takes precedence
# over the per-zone routing. The VERP bounce regexp comes first, so asynchronous bounces
# accepted on the MX are handed to the events service.
_relay_cf recipient_transport.cf "SELECT IF(r.smtp_host='direct', 'smtp:', CONCAT('smtp:[', r.smtp_host, ']:', r.smtp_port)) FROM recipient_relays rr JOIN relays r ON r.id=rr.relay_id WHERE rr.recipient_domain='%s' AND rr.is_active=1"
postconf -e \
    "sender_dependent_relayhost_maps=mysql:/etc/postfix/relay_by_zone.cf" \
    "transport_maps=hash:/etc/postfix/transport,regexp:/etc/postfix/bounce_transport.regexp,mysql:/etc/postfix/recipient_transport.cf" \
    "smtp_sasl_auth_enable=yes" \
    "smtp_sasl_password_maps=mysql:/etc/postfix/relay_sasl.cf" \
    "smtp_sasl_security_options=noanonymous" \
    "smtp_sasl_mechanism_filter=plain,login" \
    "smtp_tls_policy_maps=mysql:/etc/postfix/relay_tls.cf"

# Outbound pace per destination: moderate concurrency + connection reuse. Mailbox providers
# (Gmail, Outlook…) penalize bursts from a new IP; per-IP warm-up is handled by the jobs.
postconf -e \
    "smtp_destination_concurrency_limit=${EGRESS_CONCURRENCY:-10}" \
    "smtp_connection_cache_on_demand=yes" \
    "smtp_connection_reuse_time_limit=${EGRESS_REUSE_TIME:-300s}" \
    "smtp_destination_concurrency_failed_cohort_limit=${EGRESS_FAILED_COHORT:-10}" \
    "smtp_connect_timeout=${EGRESS_CONNECT_TIMEOUT:-15s}" \
    "anvil_rate_time_unit=60s"

# local testing: deliver everything to one relay (Mailpit). Empty in production = straight to the MX.
if [ -n "${RELAYHOST:-}" ]; then
    postconf -e "relayhost=${RELAYHOST}"
    log "relayhost=${RELAYHOST} (local testing only)"
fi

log "postfix: $(postconf -h myhostname), internal queue :${QUEUE_SUBMISSION_PORT:-2587}, direct delivery to the MX"
exec /usr/bin/supervisord -c /etc/supervisor/connect-supervisord.conf
