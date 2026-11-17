# Getting started

This runs the whole platform on your machine. The development override makes the MTA hand
every message to [Mailpit](https://mailpit.axllent.org/) (a fake inbox), so nothing is ever
delivered to the internet.

## Requirements

- Docker with Compose v2 (Docker Desktop on macOS/Windows, or Docker Engine on Linux), with at
  least 4 GB of memory available to containers.
- Python 3.11 or newer on the host, only to render the configuration (`make env`). No Python
  packages are needed.
- `make`, `openssl` and `curl`.

## 1. Render the configuration

```bash
make env
```

This reads `connect.toml`, generates the secrets once (`secrets/generated.env`, mode 0600,
never committed) and writes one environment file per service group under `secrets/env/`.
The defaults use `example.com`; that is fine locally.

## 2. Build and start

```bash
make build   # app, milter, MTA, image proxy and the Caddy image with the panel
make dev     # docker compose -f docker-compose.yml -f docker-compose.dev.yml up -d
```

Local endpoints:

| URL | What |
|---|---|
| http://localhost:8001 | HTTP API (`/send`, `/health`, interactive docs at `/docs`) |
| http://localhost:8088 | control panel |
| http://localhost:8025 | Mailpit, where every message ends up |
| http://localhost:8002 | tracking (pixel, clicks, unsubscribe) |

### Optional: a demo store

To see the panel full of data before creating your own customer:

```bash
make demo
```

It creates the "Acme Store" customer (`acme.example`) with a signed webhook, sends about 90
emails through the real pipeline (Mailpit refuses the `old.…` addresses, so a few bounce for
real), opens and clicks some of them, and writes 40 days of synthetic history for the charts.
It prints the panel login; the API key and the DNS records are in
`/tmp/connect-demo/bootstrap.json`. Run it once on a fresh stack (`make clean-local`, then
`make dev`).

## 3. Create a customer

A customer owns sending domains, users and API keys. The bootstrap script creates all of it,
including the DKIM keys:

```bash
docker compose exec api python scripts/bootstrap_tenant.py \
  --domain shop.example --customer "Example Shop" \
  --admin-email you@shop.example --admin-password 'choose-a-password'
```

It prints JSON with the API key (shown only once), the panel user, the return-path (VERP)
domain, the feedback-loop address and the DNS records to publish. Keep the output.

## 4. Send an email

```bash
export API_KEY=...   # from the bootstrap output
curl -sS -X POST http://localhost:8001/send \
  -H "Authorization: Bearer $API_KEY" -H "Content-Type: application/json" \
  -d '{
        "personalizations": [{"to": [{"name": "Ana", "address": "ana@example.org"}],
                              "substitutions": {"first_name": "Ana"}}],
        "from": {"name": "Example Shop", "address": "news@shop.example"},
        "subject": "Welcome, {{first_name}}",
        "content": [
          {"type": "text/plain", "value": "Hi {{first_name}}! https://example.org/welcome"},
          {"type": "text/html", "value": "<p>Hi {{first_name}}! <a href=\"https://example.org/welcome\">Start here</a></p>"}
        ]
      }'
```

The response contains a `message_id`. Then:

- Mailpit shows the message with its `DKIM-Signature`, the VERP `Return-Path`, rewritten
  links, the open pixel and the `List-Unsubscribe` headers.
- `GET /status/{message_id}` (same bearer token) moves from `queued` to `delivered`.
- The panel lists the email with its journey (queued, accepted by the MTA, delivered, opened…).

## 5. Run the test suites

```bash
make test         # fresh stack: e2e, node failover, panel and isolation, webhooks, complaints
make test-unit    # unit tests inside the app image
make loadtest     # throughput on this machine (docs/scaling.md)
```

`make test` deletes the local Connect volumes first. It only ever talks to the local stack.

## Clean up

```bash
make clean-local   # stops everything, deletes volumes and the local images
```

Next: [Installation](installation.md) to run it on a real server.
