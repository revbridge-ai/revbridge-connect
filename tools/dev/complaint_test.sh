#!/usr/bin/env bash
# Spam-complaint test (ARF/FBL): real send -> ARF on the MX -> suppression + webhook + panel.
# Requires the dev stack up and customer A bootstrapped (tools/dev/run_e2e.sh). Local Docker only.
#   bash tools/dev/complaint_test.sh <customer A's bootstrap.json>
set -uo pipefail
cd "$(dirname "$0")/../.."
export COMPOSE_FILE=docker-compose.yml:docker-compose.dev.yml
BOOT="${1:?usage: complaint_test.sh <bootstrap.json>}"
docker compose up --detach webhook-sink webhook-dispatcher >/dev/null 2>&1
docker compose cp tools/dev/complaint_check.py api:/tmp/complaint_check.py >/dev/null
docker compose cp "$BOOT" api:/tmp/bootstrap.json >/dev/null
docker compose cp tools/dev/arf_inject.py mta:/tmp/arf_inject.py >/dev/null
INFO=$(docker compose exec -T api python /tmp/complaint_check.py prepare /tmp/bootstrap.json | tail -1)
J() { python3 -c 'import json,sys;print(json.loads(sys.argv[1])[sys.argv[2]])' "$INFO" "$1"; }
docker compose exec -T mta python3 /tmp/arf_inject.py "$(J rp)" "$(J rcpt)" "$(J message_id)"
docker compose exec -T mta python3 /tmp/arf_inject.py "$(J rp)" "$(J forged_rcpt)" "00000000-0000-4000-8000-000000000000"
docker compose exec -T api python /tmp/complaint_check.py verify /tmp/bootstrap.json "$INFO"
