#!/usr/bin/env bash
# Creates the first customer on the installed server and prints EVERY DNS record in one list.
#
#   scripts/first_customer.sh root@203.0.113.10 --domain acme.com --customer "Acme" --admin-email you@acme.com
#   (SSH_KEY=... and CONNECT_TOML=... as in deploy.sh)
#
# Runs scripts/bootstrap_tenant.py on the server over SSH and saves its output in
# ~/.config/revbridge-connect/bootstrap-<domain>.json (mode 600). That file holds the API key and
# the panel password: this script never prints them. It then runs `dns_records.py plan`, which
# finds where your DNS is hosted and prints the records for the installation and for the customer
# in one table, plus a zone file to import.
set -euo pipefail

TARGET="${1:?usage: scripts/first_customer.sh user@ip --domain D --customer NAME --admin-email E}"
shift
DOMAIN="" ; CUSTOMER="" ; EMAIL=""
while [ $# -gt 0 ]; do
  case "$1" in
    --domain) DOMAIN="$2"; shift 2 ;;
    --customer) CUSTOMER="$2"; shift 2 ;;
    --admin-email) EMAIL="$2"; shift 2 ;;
    *) echo "unknown option: $1"; exit 1 ;;
  esac
done
[ -n "$DOMAIN" ] && [ -n "$CUSTOMER" ] && [ -n "$EMAIL" ] || { echo "--domain, --customer and --admin-email are required"; exit 1; }

case "$DOMAIN" in *[!a-zA-Z0-9.-]*|"") echo "--domain must be a plain domain name"; exit 1 ;; esac
case "$CUSTOMER$EMAIL" in *"'"*) echo "no single quotes in --customer or --admin-email"; exit 1 ;; esac

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
SSH_OPTS=(-o StrictHostKeyChecking=accept-new)
if [ -n "${SSH_KEY:-}" ]; then SSH_OPTS+=(-i "$SSH_KEY" -o IdentitiesOnly=yes); fi
OUT_DIR="$HOME/.config/revbridge-connect"
OUT="${OUT_DIR}/bootstrap-${DOMAIN}.json"
LATEST="$(ls -t "$HOME"/.config/revbridge-connect/connect-*.toml 2>/dev/null | head -1)"   # the newest manifest init_manifest.py wrote
MANIFEST="${CONNECT_TOML:-${LATEST:-${ROOT}/connect.toml}}"

mkdir -p "$OUT_DIR"
chmod 700 "$OUT_DIR"
if [ -s "$OUT" ]; then
  echo "==> ${OUT} already exists: using it (the bootstrap is idempotent, but the API key is shown only once)"
else
  echo "==> creating the customer on ${TARGET}"
  umask 077
  ssh "${SSH_OPTS[@]}" "$TARGET" "cd /opt/revbridge-connect && docker compose exec -T api python scripts/bootstrap_tenant.py \
    --domain '${DOMAIN}' --customer '${CUSTOMER}' --admin-email '${EMAIL}'" > "${OUT}.tmp"
  python3 -c 'import json,sys; json.load(open(sys.argv[1]))' "${OUT}.tmp" \
    || { echo "the bootstrap did not return JSON (see above); nothing was saved"; rm -f "${OUT}.tmp"; exit 1; }
  mv "${OUT}.tmp" "$OUT"
  chmod 600 "$OUT"
fi

PANEL="$(python3 -c 'import tomllib,sys; print(tomllib.load(open(sys.argv[1],"rb"))["endpoints"]["panel"])' "$MANIFEST")"
echo
echo "Customer created. The API key and the panel password are in ${OUT} (only you can read it)."
echo "Panel: https://${PANEL}  (login: ${EMAIL})"
echo
echo "==> DNS records, all at once"
python3 "${ROOT}/scripts/dns_records.py" plan --manifest "$MANIFEST" --bootstrap "$OUT"
echo
echo "After you publish them: scripts/go_live.sh ${TARGET} (checks the DNS, starts the certificates, runs the full preflight)."
