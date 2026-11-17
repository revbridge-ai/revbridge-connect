#!/usr/bin/env bash
# Webhook test (dispatcher + local receiver). Requires the dev stack up and customer A
# bootstrapped (tools/dev/run_e2e.sh leaves it that way). Creates customer B if missing. Local Docker only.
#   bash tools/dev/webhook_test.sh <customer A's bootstrap.json>
set -uo pipefail
cd "$(dirname "$0")/../.."
export COMPOSE_FILE=docker-compose.yml:docker-compose.dev.yml
BOOT="${1:?usage: webhook_test.sh <bootstrap.json>}"
OUT="$(dirname "$BOOT")"
docker compose up --detach webhook-sink webhook-dispatcher >/dev/null 2>&1
docker compose exec -T api python scripts/bootstrap_tenant.py \
  --domain other-shop.example --customer "Other Shop" --admin-email admin@other-shop.example \
  --admin-password "Test-B-$(date +%s)" > "$OUT/bootstrap_b.new" 2>"$OUT/bootstrap_b.err" \
  || { cat "$OUT/bootstrap_b.err"; exit 1; }
# on the same stack B's user already exists and its password is not printed again: keep the previous file
grep -q "user already existed" "$OUT/bootstrap_b.new" || mv "$OUT/bootstrap_b.new" "$OUT/bootstrap_b.json"
docker compose cp tools/dev/webhook_test.py api:/tmp/webhook_test.py >/dev/null
docker compose cp "$BOOT" api:/tmp/bootstrap.json >/dev/null
docker compose cp "$OUT/bootstrap_b.json" api:/tmp/bootstrap_b.json >/dev/null
docker compose exec -T api python /tmp/webhook_test.py /tmp/bootstrap.json /tmp/bootstrap_b.json
