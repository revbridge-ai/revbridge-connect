#!/usr/bin/env bash
# Local test of the automatic node switch (blocklist -> quarantine -> back). Requires the dev stack
# already up and bootstrapped (tools/dev/run_e2e.sh leaves it that way). Local Docker only.
#   bash tools/dev/node_failover_test.sh <bootstrap.json>
set -uo pipefail
cd "$(dirname "$0")/../.."
export COMPOSE_FILE=docker-compose.yml:docker-compose.dev.yml
BOOT="${1:?usage: node_failover_test.sh <bootstrap.json>}"
docker compose cp tools/dev/node_test.py api:/tmp/node_test.py >/dev/null
docker compose cp "$BOOT" api:/tmp/bootstrap.json >/dev/null
T() { docker compose exec -T api python /tmp/node_test.py "$@" 2>/dev/null | tail -1; }
reload_mta() { docker compose exec -T mta postfix reload >/dev/null 2>&1; }   # clears Postfix's route cache
health() { docker compose exec -T "$@" scheduler python scripts/scheduler.py --once node_health 2>/dev/null | grep -E '"event": "(node_check|zone_moved|default_moved|no_target)"' | cut -c1-220; }
PASS=0; FAIL=0
check() { if [ "$2" = "$3" ]; then echo "OK    $1 ($2)"; PASS=$((PASS+1)); else echo "FAIL  $1 (expected $3, got $2)"; FAIL=$((FAIL+1)); fi; }

NODE_IP=$(T setup /tmp/bootstrap.json | python3 -c 'import json,sys;print(json.load(sys.stdin)["node_ip"])')
echo "simulated node: node-sim (${NODE_IP})"
reload_mta

S1="route-1-$(date +%s)"; T send /tmp/bootstrap.json "$S1" >/dev/null
check "zone points to the node: mail leaves through the node" "$(T where "$S1")" "node"

echo "-- the node's IP gets blocklisted (simulated) --"
health -e "NH_FORCE_LISTED=${NODE_IP}"
T state
reload_mta
S2="route-2-$(date +%s)"; T send /tmp/bootstrap.json "$S2" >/dev/null
check "node listed: mail leaves through another node (control)" "$(T where "$S2")" "control"

echo "-- IP clean again: 3 checks --"
for i in 1 2 3; do health; done
T state
reload_mta
S3="route-3-$(date +%s)"; T send /tmp/bootstrap.json "$S3" >/dev/null
check "node clean: the zone goes back to the node" "$(T where "$S3")" "node"

# leave the stack as it was: the zone goes back to the control node (otherwise the next tests
# deliver to the simulated node's Mailpit)
T cleanup /tmp/bootstrap.json >/dev/null
reload_mta

echo; echo "${PASS} passed, ${FAIL} failed"
[ "$FAIL" -eq 0 ]
