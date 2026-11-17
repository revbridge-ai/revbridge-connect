#!/usr/bin/env bash
# Load test of the local dev stack (Docker only; the MTA delivers to Mailpit).
#   bash tools/dev/loadtest.sh <bootstrap.json> [messages] [batch] [concurrency]
# Requires the stack up and bootstrapped (tools/dev/run_e2e.sh leaves it that way).
set -euo pipefail
cd "$(dirname "$0")/../.."
export COMPOSE_FILE=docker-compose.yml:docker-compose.dev.yml
BOOT="${1:?usage: loadtest.sh <bootstrap.json> [messages] [batch] [concurrency]}"
MESSAGES="${2:-10000}"
BATCH="${3:-50}"
CONC="${4:-16}"
docker compose cp tools/dev/loadtest.py api:/tmp/loadtest.py >/dev/null
docker compose cp "$BOOT" api:/tmp/bootstrap.json >/dev/null
echo "queue processors: $(docker compose ps -q queue-processor | wc -l | tr -d ' ')"
docker compose exec -T api python /tmp/loadtest.py /tmp/bootstrap.json \
    --messages "$MESSAGES" --batch "$BATCH" --concurrency "$CONC"
