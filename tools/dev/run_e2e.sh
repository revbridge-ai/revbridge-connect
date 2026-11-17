#!/usr/bin/env bash
# Starts Connect from scratch in local Docker (dev: delivers to Mailpit) and runs the end-to-end tests.
#   bash tools/dev/run_e2e.sh
# Uses fresh volumes (deletes Connect's volumes first). Never points at production.
set -uo pipefail
cd "$(dirname "$0")/../.."
export COMPOSE_FILE=docker-compose.yml:docker-compose.dev.yml
OUT="${E2E_OUT:-/tmp/connect-e2e}"
mkdir -p "$OUT"
PASS="Local-Test-$(date +%s)"

python3 tools/manifest/scan_env.py --check
python3 scripts/render_env.py >/dev/null
docker compose down --volumes --remove-orphans >/dev/null 2>&1
docker compose up --detach >/dev/null 2>&1
echo "waiting for the API..."
until curl -fsS http://127.0.0.1:8001/health >/dev/null 2>&1; do sleep 2; done

docker compose exec -T api python scripts/bootstrap_tenant.py \
  --domain shop.example --customer "Example Shop" --admin-email admin@shop.example \
  --admin-password "$PASS" > "$OUT/bootstrap.json" 2>"$OUT/bootstrap.err" || { cat "$OUT/bootstrap.err"; exit 1; }
for f in connect_e2e.py tracking_test.py db_peek.py api_get.py; do docker compose cp "tools/dev/$f" "api:/tmp/$f" >/dev/null; done
docker compose cp "$OUT/bootstrap.json" api:/tmp/bootstrap.json >/dev/null

echo "== e2e"
docker compose exec -T api python /tmp/connect_e2e.py /tmp/bootstrap.json "$PASS" 2>&1 | grep -E "^(OK|FAIL)|/[0-9]+ passed"
RC=${PIPESTATUS[0]}

echo "== asynchronous bounce (DSN 5.1.1 on the MX)"
RP=$(python3 -c 'import json,sys;print(json.load(open(sys.argv[1]))["return_path"])' "$OUT/bootstrap.json")
docker compose cp tools/dev/bounce_test.py mta:/tmp/bounce_test.py >/dev/null
docker compose cp tools/dev/bounce_check.py api:/tmp/bounce_check.py >/dev/null
TS=$(date +%s)
REAL="bounce-real-${TS}@example.net"
FORGED="bounce-forged-${TS}@example.net"
MID=$(docker compose exec -T api python /tmp/bounce_check.py send /tmp/bootstrap.json "$REAL" 2>/dev/null | tail -1 \
  | python3 -c 'import json,sys;print(json.load(sys.stdin)["message_id"])')
docker compose exec -T mta python3 /tmp/bounce_test.py "$RP" "$REAL" "$MID" >/dev/null
docker compose exec -T mta python3 /tmp/bounce_test.py "$RP" "$FORGED" "00000000-0000-4000-8000-$(printf '%012d' "$TS")" >/dev/null
BOUNCE_FAIL=0
GOT=$(docker compose exec -T api python /tmp/bounce_check.py suppressed "$REAL" 2>/dev/null | tail -1)
if [ "$GOT" = "bounce" ] || [ "$GOT" = "hard" ]; then echo "OK    DSN for a real send suppresses the recipient ($GOT)"; else echo "FAIL  DSN for a real send suppresses the recipient (got $GOT)"; BOUNCE_FAIL=1; fi
GOT=$(docker compose exec -T api python /tmp/bounce_check.py suppressed "$FORGED" 2>/dev/null | tail -1)
if [ "$GOT" = "none" ]; then echo "OK    forged DSN (message never sent) is ignored"; else echo "FAIL  forged DSN (message never sent) is ignored (got $GOT)"; BOUNCE_FAIL=1; fi
[ "$RC" -eq 0 ] && [ "$BOUNCE_FAIL" -eq 0 ]
