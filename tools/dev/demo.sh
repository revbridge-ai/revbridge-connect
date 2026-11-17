#!/usr/bin/env bash
# Fills a FRESH local stack (`make dev`) with demo data, so you can explore the panel:
#   - the "Acme Store" tenant (acme.example) with its panel login and API key
#   - a signed webhook to the dev receiver
#   - ~90 real sends through the pipeline (a few bounce on purpose), with opens and clicks
#   - 40 days of synthetic history for the charts, rates and suppressions
# Then open http://localhost:8088 and log in with the credentials printed at the end.
# Local Docker only: everything is delivered to Mailpit, nothing leaves your machine.
set -euo pipefail
cd "$(dirname "$0")/../.."

COMPOSE=(docker compose -f docker-compose.yml -f docker-compose.dev.yml -f tools/dev/demo.override.yml)
OUT=${DEMO_DIR:-/tmp/connect-demo}
DAYS=${DEMO_DAYS:-40}
mkdir -p "$OUT"
chmod 700 "$OUT"

if [ -s "$OUT/bootstrap.json" ]; then
  echo "demo: $OUT/bootstrap.json exists; the demo runs once per fresh stack (make clean-local, then make dev)" >&2
  exit 1
fi

echo "demo: Mailpit now refuses non-demo recipients (some sends will bounce)"
"${COMPOSE[@]}" up -d mailpit > /dev/null 2>&1

echo "demo: creating the Acme Store tenant"
PASS="Demo-$(openssl rand -hex 6)"
umask 077
"${COMPOSE[@]}" exec -T api python scripts/bootstrap_tenant.py --domain acme.example \
  --customer "Acme Store" --admin-email admin@acme.example --admin-password "$PASS" > "$OUT/bootstrap.json"

"${COMPOSE[@]}" cp tools/dev/demo_traffic.py api:/tmp/demo_traffic.py > /dev/null 2>&1
"${COMPOSE[@]}" cp tools/dev/demo_seed.py api:/tmp/demo_seed.py > /dev/null 2>&1

echo "demo: sending, opening and clicking (about a minute)"
"${COMPOSE[@]}" exec -T api python /tmp/demo_traffic.py - < "$OUT/bootstrap.json"
echo "demo: writing $DAYS days of history"
"${COMPOSE[@]}" exec -T api python /tmp/demo_seed.py - "$DAYS" < "$OUT/bootstrap.json"

cat <<EOF

Demo ready: http://localhost:8088
  email:    admin@acme.example
  password: $PASS
Inbox (Mailpit): http://localhost:8025
API key and DNS records: $OUT/bootstrap.json
EOF
