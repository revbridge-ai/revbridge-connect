#!/usr/bin/env bash
# Looks at a VPS BEFORE you install on it. Read-only: it changes nothing on the server.
#
#   scripts/check-vps.sh root@203.0.113.10
#   SSH_KEY=~/.ssh/my_vps_key scripts/check-vps.sh root@203.0.113.10
#
# Checks: SSH access, operating system, privileges, CPU/RAM/disk, outbound port 25 (the one that
# matters: many providers block it), the DNS resolver, whether ports 25/80/443 are free, whether
# there is an installation already, and the IP's current reverse DNS.
# Exit code 1 when something blocks the installation; warnings do not block.
set -uo pipefail

TARGET="${1:?usage: scripts/check-vps.sh user@ip}"
IP="${TARGET#*@}"
SSH_OPTS=(-o BatchMode=yes -o ConnectTimeout=15 -o StrictHostKeyChecking=accept-new)
if [ -n "${SSH_KEY:-}" ]; then SSH_OPTS+=(-i "$SSH_KEY" -o IdentitiesOnly=yes); fi

echo "Checking ${TARGET}"
if ! ssh "${SSH_OPTS[@]}" "$TARGET" true 2>/dev/null; then
  echo "  FAIL  cannot log in over SSH as ${TARGET} (wrong user, key or IP; with a password-only server add your key first: ssh-copy-id)"
  exit 1
fi
echo "  ok    SSH login works"

# The remote part prints "level|message" lines; the local part only formats them.
read -r -d '' REMOTE_SCRIPT <<'REMOTE' || true
say() { echo "$1|$2"; }
. /etc/os-release 2>/dev/null
case "${ID:-}-${VERSION_ID:-}" in
  ubuntu-22.04|ubuntu-24.04|debian-12) say ok "operating system ${PRETTY_NAME}" ;;
  *) say warn "operating system ${PRETTY_NAME:-unknown}: the installer supports Ubuntu 22.04/24.04 and Debian 12" ;;
esac
if [ "$(id -u)" -eq 0 ]; then say ok "logged in as root"; \
elif sudo -n true 2>/dev/null; then say ok "sudo without a password"; \
else say FAIL "not root and no passwordless sudo: the installer must run as root"; fi

cpu="$(nproc)"; mem="$(awk '/MemTotal/ {printf "%d", $2/1024/1024 + 0.5}' /proc/meminfo)"
disk="$(df -BG --output=avail / | tail -1 | tr -dc 0-9)"
[ "$cpu" -ge 2 ] && say ok "${cpu} vCPU" || say warn "${cpu} vCPU: 2 is the practical minimum, 4 is comfortable"
[ "$mem" -ge 4 ] && say ok "${mem} GB RAM" || say warn "${mem} GB RAM: the build and MySQL want at least 4 GB (8 is comfortable)"
[ "$disk" -ge 30 ] && say ok "${disk} GB free disk" || say FAIL "${disk} GB free disk: at least 30 GB are needed"

# Outbound 25: a plain TCP connect. The greeting is not read, so a slow banner is not a false alarm.
open=""
for h in gmail-smtp-in.l.google.com mx.zoho.com; do
  if timeout 10 bash -c "exec 3<>/dev/tcp/${h}/25" 2>/dev/null; then open="$h"; break; fi
done
[ -n "$open" ] && say ok "outbound port 25 is open (connected to ${open})" \
  || say FAIL "outbound port 25 is blocked: no direct delivery is possible. Ask the provider to unblock it, or choose another provider"

ns="$(awk '/^nameserver/ {print $2}' /etc/resolv.conf | tr '\n' ' ')"
if command -v resolvectl >/dev/null 2>&1; then ns="$ns$(resolvectl dns 2>/dev/null | awk -F: '{print $2}' | tr '\n' ' ')"; fi
ns="$(echo $ns | tr ' ' '\n' | sort -u | tr '\n' ' ' | sed 's/ $//')"
case " $ns " in
  *" 8.8.8.8 "*|*" 8.8.4.4 "*|*" 1.1.1.1 "*|*" 1.0.0.1 "*|*" 9.9.9.9 "*|*" 208.67.222.222 "*)
    say warn "this server uses a public DNS resolver (${ns}): Spamhaus refuses those, so the preflight cannot check that blocklist. Check the IP by hand at https://check.spamhaus.org/" ;;
  *) say ok "DNS resolver: ${ns:-none found}" ;;
esac
getent hosts example.com >/dev/null 2>&1 && say ok "DNS lookups work" || say FAIL "this server cannot resolve names (see /etc/resolv.conf)"

for p in 25 80 443; do
  who="$(ss -ltnpH "sport = :$p" 2>/dev/null | grep -o '"[^"]*"' | head -1 | tr -d '"')"
  if [ -z "$who" ]; then say ok "port ${p} is free"
  elif [ "$who" = "docker-proxy" ] && [ -d /opt/revbridge-connect ]; then say ok "port ${p} is held by the existing installation"
  else say warn "port ${p} is used by ${who}: stop it first, Connect needs this port"; fi
done

if [ -d /opt/revbridge-connect ]; then say warn "there is an installation in /opt/revbridge-connect: deploying again updates it and keeps its data"; fi
command -v docker >/dev/null 2>&1 && say ok "Docker is installed" || say ok "Docker is not installed (the installer adds it)"

ptr="$(getent hosts "$EXPECTED_IP" 2>/dev/null | awk '{print $2}' | head -1)"
say ok "reverse DNS today: ${ptr:-none} (you set it to mta1.<your installation domain> in the provider's panel; it can wait until the deploy is building)"
REMOTE
OUT="$(ssh "${SSH_OPTS[@]}" "$TARGET" "EXPECTED_IP='${IP}' bash -s" <<<"$REMOTE_SCRIPT")"

FAILED=0
while IFS='|' read -r level msg; do
  [ -z "$level" ] && continue
  printf '  %-5s %s\n' "$level" "$msg"
  [ "$level" = "FAIL" ] && FAILED=1
done <<<"$OUT"

if [ "$FAILED" -eq 1 ]; then
  echo "Something blocks the installation (FAIL above)."
  exit 1
fi
echo "This server can be installed. Warnings are worth reading but do not block."
