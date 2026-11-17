# Configuration

Connect has one file you edit and a few you never touch.

| File | Who writes it | What it is |
|---|---|---|
| `connect.toml` | you | the installation manifest: domain, hostnames, IP, scale, nodes, jobs |
| `config/variables.toml` | `make catalog` | catalog of **every** environment variable the code reads, with group, status, default and the services that read it |
| `config/variables.overrides.toml` | maintainers | curated values: what is derived from `connect.toml`, what is a secret, deliberate defaults |
| `secrets/generated.env` | `make env` (once) | generated passwords and keys, mode 0600, never committed |
| `secrets/env/*.env` | `make env` | one environment file per service group (see below) |
| `.env` | `make env` | only what `docker compose` interpolates into `docker-compose.yml` |

Run `make env` after every change to `connect.toml`, then `docker compose up -d` to apply.

## `connect.toml`

### `[install]`

| Key | Meaning |
|---|---|
| `name` | short installation name |
| `domain` | base domain; Message-IDs and the installation zone live under it |
| `hostname` | FQDN of the control node; HELO name and the PTR of `public_ip` |
| `public_ip` | public IPv4 of the control node (required in production) |
| `admin_email` | Let's Encrypt account email and DMARC aggregate report address |
| `timezone` | keep `UTC` (services and MySQL must agree) |

### `[endpoints]`

Public hostnames for the API, tracking, SMTP submission, bounce MX, image proxy and panel. Each
needs an A record to the control node. See [Installation](installation.md#2-plan-the-names).

### `[mysql]`, `[redis]`

Defaults point at the bundled `db` and `redis` services. You can point `host` at a managed
MySQL 8 / Redis 7 instead; keep Redis persistence on (the send queue lives there).

### `[delivery]`

`mode = "direct"` is the only mode: mail goes to the recipient's MX from your node IPs.
`dkim_selectors` names the two rotating DKIM selectors.

### `[scale]`

| Key | Default | Effect |
|---|---|---|
| `queue_processors` | 8 | sequential senders that pull from the queue and submit to the MTA; throughput grows with this number |
| `api_workers` | 2 | uvicorn worker processes of the API |
| `tracking_workers` | 2 | uvicorn workers of the tracking service |
| `events_workers` | 2 | uvicorn workers of the delivery-event ingester |

See [Scaling](scaling.md) for measured numbers.

### `[[nodes]]`

The sending nodes. Node 0 is the control VM, which also sends.

### `[deliverability]`

`dnsbl_resolver` and one `[deliverability.jobs.<name>]` table per scheduled job (`script`,
cron `schedule`, `enabled`, `timeout` in seconds). See [Deliverability](deliverability.md#scheduled-jobs).

## Variables

`config/variables.toml` is generated from the code (`make catalog`), so it always lists every
variable that some service reads. `make check` (also run by `make env`) fails if the code reads
a variable that is not in the catalog. Each entry has a `status`:

| Status | Meaning |
|---|---|
| `derive` | computed from `connect.toml` (hostnames, URLs, ports) |
| `secret` | taken from `secrets/generated.env` |
| `keep` | a fixed value chosen for Connect |
| `off` | a feature that is disabled in Connect |
| `remove` / `internal` | never written to the environment |

Entries without a value use the default in the code. To change a value, add or edit its entry
in `config/variables.overrides.toml` and run `make catalog && make env`.

## Secrets and environment files

`make env` generates each secret once and reuses it on later runs:

| Secret | Used for |
|---|---|
| `DB_PASSWORD` | application database user |
| `MTA_DB_PASSWORD` | the MTA's **read-only** database user |
| `MYSQL_ROOT_PASSWORD` | only the `db` container |
| `REDIS_PASSWORD` | queue and caches |
| `TOKEN_ENCRYPTION_KEY` | Fernet key of tracking tokens (rotating it invalidates links already sent) |
| `JWT_SECRET_KEY` | panel sessions |
| `IMAGE_PROXY_SECRET` | HMAC of image-proxy URLs |

Each container receives `secrets/env/common.env` (no secrets) plus the file of its scope:

| Scope file | Containers | Secrets |
|---|---|---|
| `app.env` | api, queue-processor, ledger-reaper, webhook-dispatcher, scheduler | database, Redis, tracking key, JWT, image-proxy HMAC |
| `tracking.env` | tracking | database, Redis, tracking key |
| `events.env` | events, milter | database, Redis, tracking key |
| `mta.env` | mta | read-only database user only |
| `proxy.env` | image-proxy | image-proxy HMAC only |

Caddy and Vector get no secrets at all. Back up `secrets/` together with the database: without
`TOKEN_ENCRYPTION_KEY`, links in emails already sent stop working.
