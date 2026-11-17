#!/usr/bin/env bash
# Runs ON THE VM (Ubuntu 22.04/24.04 or Debian 12), as root, inside /opt/revbridge-connect.
# Idempotent: run it again to update.
set -euo pipefail
cd "$(dirname "$0")/.."

if [ "$(id -u)" -ne 0 ]; then echo "run as root"; exit 1; fi

HOSTNAME_FQDN="$(python3 -c 'import tomllib;print(tomllib.load(open("connect.toml","rb"))["install"]["hostname"])' 2>/dev/null || true)"
if [ -z "$HOSTNAME_FQDN" ]; then
  apt-get update -qq && apt-get install -y -qq python3 >/dev/null
  HOSTNAME_FQDN="$(python3 -c 'import tomllib;print(tomllib.load(open("connect.toml","rb"))["install"]["hostname"])')"
fi

case "$HOSTNAME_FQDN" in
  *example.com|*example.org|"")
    echo "connect.toml still has the example names (${HOSTNAME_FQDN:-empty})."
    echo "Write your own manifest: scripts/init_manifest.py --ip IP --domain DOMAIN --email EMAIL"
    exit 1 ;;
esac

echo "==> base packages"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq ca-certificates curl gnupg make python3 openssl dnsutils ufw rsync >/dev/null

if ! command -v docker >/dev/null 2>&1; then
  echo "==> Docker (official repository)"
  install -m 0755 -d /etc/apt/keyrings
  . /etc/os-release
  curl -fsSL "https://download.docker.com/linux/${ID}/gpg" | gpg --dearmor -o /etc/apt/keyrings/docker.gpg
  chmod a+r /etc/apt/keyrings/docker.gpg
  echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] https://download.docker.com/linux/${ID} ${VERSION_CODENAME} stable" \
    > /etc/apt/sources.list.d/docker.list
  apt-get update -qq
  apt-get install -y -qq docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin >/dev/null
  systemctl enable --now docker
fi

echo "==> hostname = ${HOSTNAME_FQDN} (must match the IP's PTR)"
hostnamectl set-hostname "${HOSTNAME_FQDN}"

echo "==> firewall: 22 (ssh), 25 (MX/bounces), 80/443 (API, tracking, panel), 465/587 (submission)"
ufw --force reset >/dev/null
ufw default deny incoming >/dev/null
ufw default allow outgoing >/dev/null
for p in 22 25 80 443 465 587; do ufw allow "${p}/tcp" >/dev/null; done
ufw --force enable >/dev/null

echo "==> preflight (outbound port 25 and blocklists; the DNS checks come after you publish the records)"
PREFLIGHT_OUT="$(mktemp)"
bash scripts/preflight.sh --before-dns 2>&1 | tee "$PREFLIGHT_OUT" || true
if grep -q "^  FAIL" "$PREFLIGHT_OUT"; then
  echo "WARNING: the preflight found something that blocks delivery (FAIL above). The installation continues so you can look at it."
fi

echo "==> .env and secrets (generated here, they never leave the VM)"
make env

# Prebuilt images (optional): CONNECT_IMAGE_REGISTRY=ghcr.io/ORG CONNECT_VERSION=1.0.0 skips the build.
USE_PREBUILT=0
if [ -n "${CONNECT_IMAGE_REGISTRY:-}" ] && [ -n "${CONNECT_VERSION:-}" ]; then
  USE_PREBUILT=1
  for img in app image-proxy milter mta caddy; do
    if docker pull -q "${CONNECT_IMAGE_REGISTRY}/connect-${img}:${CONNECT_VERSION}" >/dev/null 2>&1; then
      docker tag "${CONNECT_IMAGE_REGISTRY}/connect-${img}:${CONNECT_VERSION}" "revbridge/connect-${img}:${CONNECT_VERSION}"
    else
      echo "    no prebuilt image for ${img}: building from source instead"
      USE_PREBUILT=0
      break
    fi
  done
fi
if [ "$USE_PREBUILT" -eq 1 ]; then
  echo "==> prebuilt images ${CONNECT_VERSION} (no build)"
else
  echo "==> build (the first time takes 5 to 10 minutes; later runs reuse the cache)"
  docker compose build
fi

# Caddy asks Let's Encrypt for the certificates as soon as it starts. Without the DNS records that
# fails and retries, and Let's Encrypt limits failed attempts: so Caddy starts only when the names resolve.
IP="$(python3 -c 'import tomllib;print(tomllib.load(open("connect.toml","rb"))["install"]["public_ip"])')"
NAMES="$(python3 -c 'import tomllib;d=tomllib.load(open("connect.toml","rb"));print(" ".join(d["endpoints"].values()))')"
DNS_READY=1
for n in $NAMES; do
  [ "$(dig +short A "$n" | head -1)" = "$IP" ] || DNS_READY=0
done
NOBUILD=""
[ "$USE_PREBUILT" -eq 1 ] && NOBUILD="--no-build"
if [ "$DNS_READY" -eq 1 ]; then
  echo "==> start (the DNS already points here: Caddy gets its certificates now)"
  docker compose up --detach $NOBUILD
else
  echo "==> start, without Caddy (the DNS records are not published yet; scripts/first_customer.sh starts it later)"
  # shellcheck disable=SC2046
  docker compose up --detach $NOBUILD $(docker compose config --services | grep -vx caddy)
fi
docker compose ps

echo
echo "==> summary"
grep -E "^  (ok|warn|FAIL|later)|^Preflight|^Not verified" "$PREFLIGHT_OUT" || true
rm -f "$PREFLIGHT_OUT"

cat <<'NEXT'

Done. Next:
  1. From your machine: scripts/first_customer.sh (creates the first customer and prints ALL the
     DNS records in one list).
  2. Publish those records, then check them: scripts/dns_records.py check
  3. The PTR (reverse DNS of the IP) is set in the VPS provider's panel.
NEXT
