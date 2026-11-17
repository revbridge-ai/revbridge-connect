# RevBridge Connect — installation commands.
# Flow: edit connect.toml -> make env -> make build -> make up
COMPOSE      := docker compose
COMPOSE_DEV  := docker compose -f docker-compose.yml -f docker-compose.dev.yml
PY           := python3
BOOT         ?= /tmp/connect-e2e/bootstrap.json

.PHONY: help env catalog check build up dev demo down ps logs jobs test test-unit loadtest clean-local

help:
	@echo "make env         generate secrets and per-service env files from connect.toml"
	@echo "make build       build the images (app, milter, mta, image-proxy, caddy + panel)"
	@echo "make up          start everything (production: VM with its own IP and DNS)"
	@echo "make dev         start everything locally, delivering to Mailpit (localhost:8025)"
	@echo "make demo        fill a fresh local stack with demo data (panel on localhost:8088)"
	@echo "make test        end-to-end suites against a fresh local stack (Docker only)"
	@echo "make test-unit   unit tests inside the app image"
	@echo "make loadtest    measure sends/second on the local stack (docs/scaling.md)"
	@echo "make jobs        list the deliverability jobs and their schedule"
	@echo "make catalog     regenerate config/variables.toml from the code"
	@echo "make check       fail if the code reads a variable missing from the catalog"
	@echo "make down        stop (keeps volumes)"
	@echo "make clean-local stop, delete volumes and the local Connect images"

env: check
	$(PY) scripts/render_env.py

catalog:
	$(PY) tools/manifest/scan_env.py

check:
	$(PY) tools/manifest/scan_env.py --check

build:
	$(COMPOSE_DEV) build

up: env
	$(COMPOSE) up -d

dev: env
	$(COMPOSE_DEV) up -d

demo:
	bash tools/dev/demo.sh

down:
	$(COMPOSE) down

ps:
	$(COMPOSE) ps

logs:
	$(COMPOSE) logs -f --tail=100

jobs:
	$(COMPOSE) run --rm --no-deps scheduler python scripts/scheduler.py --list

test:
	bash tools/dev/run_e2e.sh
	bash tools/dev/node_failover_test.sh $(BOOT)
	bash tools/dev/panel_test.sh $(BOOT)
	bash tools/dev/webhook_test.sh $(BOOT)
	bash tools/dev/complaint_test.sh $(BOOT)

test-unit:
	bash tools/dev/run_unit_tests.sh

loadtest:
	bash tools/dev/loadtest.sh $(BOOT)

clean-local:
	$(COMPOSE_DEV) --profile prod-only down -v --remove-orphans
	-docker image rm revbridge/connect-app:dev revbridge/connect-milter:dev revbridge/connect-mta:dev revbridge/connect-image-proxy:dev revbridge/connect-caddy:dev
	-rm -rf /tmp/connect-demo
