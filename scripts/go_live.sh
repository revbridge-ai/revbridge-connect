#!/usr/bin/env bash
# After you published the DNS records: checks them, starts the edge (Caddy gets its certificates)
# and runs the complete preflight on the server.
#
#   scripts/go_live.sh root@203.0.113.10 [--bootstrap ~/.config/revbridge-connect/bootstrap-acme.com.json]
#   (SSH_KEY=... and CONNECT_TOML=... as in deploy.sh)
set -euo pipefail

TARGET="${1:?usage: scripts/go_live.sh user@ip [--bootstrap FILE]}"
shift
BOOTSTRAP=""
while [ $# -gt 0 ]; do
  case "$1" in
    --bootstrap) BOOTSTRAP="$2"; shift 2 ;;
    *) echo "unknown option: $1"; exit 1 ;;
  esac
done
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
SSH_OPTS=(-o StrictHostKeyChecking=accept-new)
if [ -n "${SSH_KEY:-}" ]; then SSH_OPTS+=(-i "$SSH_KEY" -o IdentitiesOnly=yes); fi
LATEST="$(ls -t "$HOME"/.config/revbridge-connect/connect-*.toml 2>/dev/null | head -1)"   # the newest manifest init_manifest.py wrote
MANIFEST="${CONNECT_TOML:-${LATEST:-${ROOT}/connect.toml}}"
if [ -z "$BOOTSTRAP" ]; then
  # the only bootstrap file, or the newest one
  BOOTSTRAP="$(ls -t "$HOME"/.config/revbridge-connect/bootstrap-*.json 2>/dev/null | head -1 || true)"
fi
[ -n "$BOOTSTRAP" ] || { echo "no bootstrap file found: run scripts/first_customer.sh first"; exit 1; }

echo "==> DNS records"
if ! python3 "${ROOT}/scripts/dns_records.py" check --manifest "$MANIFEST" --bootstrap "$BOOTSTRAP"; then
  echo
  echo "Some records are not visible yet. A new record usually shows up within minutes; wait and run this again."
  echo "('published, not propagated yet' means your DNS host has it and public resolvers are catching up.)"
  exit 1
fi

echo
echo "==> starting the edge and the certificates"
ssh "${SSH_OPTS[@]}" "$TARGET" 'cd /opt/revbridge-connect && docker compose up --detach caddy && sleep 20 && docker compose ps caddy'

echo
echo "==> full preflight on the server"
ssh "${SSH_OPTS[@]}" "$TARGET" 'cd /opt/revbridge-connect && bash scripts/preflight.sh'

PANEL="$(python3 -c 'import tomllib,sys; print(tomllib.load(open(sys.argv[1],"rb"))["endpoints"]["panel"])' "$MANIFEST")"
echo
echo "Live. Panel: https://${PANEL}. Next: send a first email to an inbox you own (the API key is in ${BOOTSTRAP})."
