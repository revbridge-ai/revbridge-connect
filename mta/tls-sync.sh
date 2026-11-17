#!/bin/bash
# RevBridge Connect — TLS certificate for SMTP (submission 587/465, MX 25 and Dovecot).
#
# Source, in this order:
#   1. the certificate Caddy issued (Let's Encrypt/ZeroSSL) for SUBMISSION_HOST, in the
#      caddy-data volume mounted read-only at /caddy-data;
#   2. secrets/tls (mounted at /etc/ssl/submission): `make env` generates a self-signed one for
#      local testing, or you put your own there.
# Copies it to /etc/ssl/connect. With --boot it only copies; without it (supervisord's cert-watch),
# it reloads Postfix and Dovecot when the certificate changed (Caddy renewal).
set -uo pipefail

MODE="${1:-}"
HOST="${SUBMISSION_HOST:-}"
DST=/etc/ssl/connect
mkdir -p "$DST"

CRT=""
KEY=""
if [ -n "$HOST" ] && [ -d /caddy-data/caddy/certificates ]; then
    for d in /caddy-data/caddy/certificates/*/"$HOST"; do
        if [ -f "$d/$HOST.crt" ] && [ -f "$d/$HOST.key" ]; then
            CRT="$d/$HOST.crt"
            KEY="$d/$HOST.key"
            break
        fi
    done
fi
if [ -z "$CRT" ] && [ -f /etc/ssl/submission/tls.crt ] && [ -f /etc/ssl/submission/tls.key ]; then
    CRT=/etc/ssl/submission/tls.crt
    KEY=/etc/ssl/submission/tls.key
fi
if [ -z "$CRT" ]; then
    echo "[tls] no certificate: none from Caddy (${HOST:-SUBMISSION_HOST is empty}) and none in secrets/tls" >&2
    [ "$MODE" = "--boot" ] && exit 1
    exit 0
fi

if cmp -s "$CRT" "$DST/fullchain.pem" && cmp -s "$KEY" "$DST/privkey.pem"; then
    exit 0
fi
install -m 644 "$CRT" "$DST/fullchain.pem"
install -m 600 "$KEY" "$DST/privkey.pem"
echo "[tls] certificate installed from $CRT"

if [ "$MODE" != "--boot" ]; then
    postfix reload >/dev/null 2>&1 || true
    doveadm reload >/dev/null 2>&1 || true
    echo "[tls] postfix and dovecot reloaded"
fi
