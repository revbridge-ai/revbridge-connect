# Development

## Repository layout

```
connect.toml            installation manifest (the only file an operator edits)
docker-compose.yml      the whole stack on one VM; docker-compose.dev.yml for local tests
docker/                 Dockerfiles: app (Python services), milter, mta, image-proxy, panel (Caddy)
services/api            mail_api: HTTP API, queue processor, ledger reaper, webhook dispatcher
services/tracking       mail_tracking: pixel, clicks, unsubscribe, bot detection
services/milter         mail_milter: the Postfix milter and the delivery-event ingester (events)
services/infra          mail_infra library and the deliverability jobs (services/infra/scripts)
services/image-proxy    image proxy (FastAPI + Pillow + CairoSVG)
shared/                 mail_shared: database helpers used by every service
libs/connect-lib/       shared library: authentication, API keys, users, rate limiting
mta/                    MTA templates (Postfix, OpenDKIM, Dovecot), renderer and entrypoint
panel/                  control panel (Vite + React + TypeScript), baked into the Caddy image
config/                 variable catalog, database schema, Caddy and Vector configuration
scripts/                env renderer, scheduler, bootstrap, deploy/install/preflight
tools/                  catalog generator and the local test suites (tools/dev)
docs/                   this documentation and the OpenAPI document
```

## Tests

Everything runs in Docker, against a fresh local stack whose MTA delivers to Mailpit.

| Command | What it covers |
|---|---|
| `make test-unit` | unit tests, inside the app image with the current source mounted |
| `bash tools/dev/run_e2e.sh` | fresh stack: send, DKIM, tracking, unsubscribe, bounces, reports, panel login, keys |
| `bash tools/dev/node_failover_test.sh <bootstrap.json>` | routing to a simulated node and back |
| `bash tools/dev/panel_test.sh <bootstrap.json>` | panel API and isolation between two customers |
| `bash tools/dev/webhook_test.sh <bootstrap.json>` | webhook delivery, signature, retries, SSRF guard |
| `bash tools/dev/complaint_test.sh <bootstrap.json>` | ARF complaint reports to suppression and webhook |
| `make loadtest` | throughput (see [Scaling](scaling.md)) |

`run_e2e.sh` writes the bootstrap output to `/tmp/connect-e2e/bootstrap.json`; `make test`
chains all of them. The suites never talk to anything outside the local Docker network.

The panel has its own checks: `cd panel && npm ci && npm run build` (type check + build).

## Variables

When code starts reading a new environment variable, run `make catalog` so it appears in
`config/variables.toml`, and give it a value in `config/variables.overrides.toml` if the
default in the code is not right for Connect. `make check` fails otherwise.

## Database schema

`config/db/initdb/*.sql` creates the schema on an empty volume, in file order. A change to the
schema needs a new numbered file and an entry in the changelog with the statement to run on
existing installations.

## Conventions

- Python 3.12+ (the milter image stays on 3.12 because of `pymilter`), formatted with `ruff`.
- New identifiers (functions, classes, files) in English. Much of the code came from a
  Portuguese-speaking team, so comments and some messages are in Portuguese; translations are
  welcome.
- Behaviour that changes what goes on the wire (SMTP headers, webhook payloads, API responses)
  needs a test and a changelog entry.
