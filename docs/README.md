# RevBridge Connect documentation

Start with [Getting started](getting-started.md) to run everything locally in a few minutes,
then [Installation](installation.md) to put a node on the internet.

| Guide | What it covers |
|---|---|
| [Getting started](getting-started.md) | Local stack, bootstrap a customer, send the first email, open the panel |
| [Installation](installation.md) | Choosing a VPS, DNS and reverse DNS, TLS, preflight, first deploy, warm-up |
| [Configuration](configuration.md) | `connect.toml`, the variable catalog, generated secrets and env files |
| [Architecture](architecture.md) | Every service, the send / event / tracking / webhook paths, storage |
| [API](api.md) | API keys and panel logins, `POST /send`, status, suppressions, reports |
| [Webhooks](webhooks.md) | Event types, payloads, headers, signature verification, retries |
| [Deliverability](deliverability.md) | Warm-up, bounces, complaints (FBL), suppressions, the scheduled jobs |
| [Scaling](scaling.md) | Measured throughput, sizing a VM, the knobs that matter |
| [Security](security.md) | Threat model, isolation between customers, hardening of each component |
| [Operations](operations.md) | Backups, upgrades, logs, metrics, troubleshooting |
| [Development](development.md) | Repository layout, running the tests, conventions |

The HTTP API is described by its OpenAPI document in [openapi/mail-api.json](openapi/mail-api.json).
