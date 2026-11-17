#!/usr/bin/env bash
# Installs/updates RevBridge Connect on a clean VM, from your own machine, over SSH only.
#
#   scripts/deploy.sh root@203.0.113.10            # uses the default ~/.ssh/id_* key
#   SSH_KEY=~/.ssh/my_vps_key scripts/deploy.sh root@203.0.113.10
#   CONNECT_TOML=~/my-install.toml scripts/deploy.sh root@203.0.113.10   # manifest kept outside the checkout
#
# First write your manifest outside the checkout:
#   scripts/init_manifest.py --ip 203.0.113.10 --domain acme.com --email you@acme.com
# and point CONNECT_TOML at the path it prints (or edit connect.toml by hand).
# Optional: CONNECT_IMAGE_REGISTRY=ghcr.io/ORG CONNECT_VERSION=1.0.0 pulls prebuilt images instead of building.
# What it does: rsyncs the repo to /opt/revbridge-connect (without .env, secrets, node_modules and .git)
# and runs scripts/install.sh there. Secrets are generated ON THE VM and never leave it.
# The install runs on the server in the background (log: /var/log/connect-install.log); this script
# only follows the log. If your connection drops, the install keeps going: run this script again and
# it reattaches instead of starting over. The last line is "deploy finished: exit N".
set -euo pipefail

TARGET="${1:?usage: scripts/deploy.sh user@ip}"
DEST="/opt/revbridge-connect"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
SSH_OPTS=(-o StrictHostKeyChecking=accept-new -o ServerAliveInterval=30)
if [ -n "${SSH_KEY:-}" ]; then SSH_OPTS+=(-i "$SSH_KEY" -o IdentitiesOnly=yes); fi

if [ -z "${CONNECT_TOML:-}" ]; then CONNECT_TOML="$(ls -t "$HOME"/.config/revbridge-connect/connect-*.toml 2>/dev/null | head -1)"; fi   # the newest one init_manifest.py wrote
MANIFEST="${CONNECT_TOML:-${ROOT}/connect.toml}"
[ -n "${CONNECT_TOML:-}" ] && echo "==> manifest: ${CONNECT_TOML}"
if [ ! -f "$MANIFEST" ]; then echo "manifest not found: ${MANIFEST}"; exit 1; fi
if grep -Eq '^hostname *= *"[^"]*example\.(com|org)"' "$MANIFEST"; then
  echo "${MANIFEST} still has the example names. Write your own:"
  echo "  scripts/init_manifest.py --ip ${TARGET#*@} --domain YOUR-DOMAIN --email YOU@YOUR-DOMAIN"
  echo "and run again with CONNECT_TOML=<the path it prints>"
  exit 1
fi

LOG="/var/log/connect-install.log"; STATUS="/var/log/connect-install.status"; PIDF="/var/log/connect-install.pid"

echo "==> checking SSH access to ${TARGET}"
ssh "${SSH_OPTS[@]}" "$TARGET" 'uname -a && (cat /etc/os-release | head -2)'

RUNNING="$(ssh "${SSH_OPTS[@]}" "$TARGET" "[ -f ${PIDF} ] && kill -0 \$(cat ${PIDF}) 2>/dev/null && [ ! -s ${STATUS} ] && echo yes || true")"
if [ "$RUNNING" = "yes" ]; then
  echo "==> an install is already running on ${TARGET}: following its log (nothing is copied or restarted)"
else
  echo "==> copying the repository to ${TARGET}:${DEST}"
  ssh "${SSH_OPTS[@]}" "$TARGET" "mkdir -p ${DEST}"
  rsync -az --delete \
    --exclude '.git/' --exclude '.env' --exclude 'secrets/' --exclude 'config/generated/' \
    --exclude 'panel/node_modules/' --exclude 'panel/dist/' --exclude '__pycache__/' --exclude '.venv/' \
    -e "ssh ${SSH_OPTS[*]}" "${ROOT}/" "${TARGET}:${DEST}/"

  if [ -n "${CONNECT_TOML:-}" ]; then
    echo "==> using the manifest ${CONNECT_TOML}"
    scp "${SSH_OPTS[@]}" "${CONNECT_TOML}" "${TARGET}:${DEST}/connect.toml"
  fi

  echo "==> installing (on the server, in the background)"
  ssh "${SSH_OPTS[@]}" "$TARGET" "cd ${DEST} && rm -f ${STATUS} && : > ${LOG} && \
    CONNECT_IMAGE_REGISTRY='${CONNECT_IMAGE_REGISTRY:-}' CONNECT_VERSION='${CONNECT_VERSION:-}' \
    nohup bash -c 'bash scripts/install.sh > ${LOG} 2>&1; echo \$? > ${STATUS}' > /dev/null 2>&1 < /dev/null & \
    echo \$! > ${PIDF}"
fi

# Follow the log. A dropped connection only pauses the display: retry for up to 10 minutes.
TMP="$(mktemp)"; trap 'rm -f "$TMP"' EXIT
OFFSET=0; FAILS=0; EXITCODE=""
while :; do
  if STATE="$(ssh "${SSH_OPTS[@]}" -o ConnectTimeout=15 "$TARGET" "cat ${STATUS} 2>/dev/null; true")" \
     && ssh "${SSH_OPTS[@]}" -o ConnectTimeout=15 "$TARGET" "tail -c +$((OFFSET + 1)) ${LOG}" > "$TMP"; then
    FAILS=0
    cat "$TMP"
    OFFSET=$((OFFSET + $(wc -c < "$TMP")))
    if [ -n "$STATE" ]; then EXITCODE="$STATE"; break; fi
  else
    FAILS=$((FAILS + 1))
    [ "$FAILS" -eq 1 ] && echo "(connection to ${TARGET} lost: the install keeps running on the server; retrying)"
    if [ "$FAILS" -ge 40 ]; then
      echo "Could not reach ${TARGET} for 10 minutes. The install continues there; run this script again to follow it."
      exit 2
    fi
  fi
  sleep 15
done
echo
echo "deploy finished: exit ${EXITCODE}"
[ "$EXITCODE" = "0" ]
