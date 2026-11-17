# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
[Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added

- First public release of RevBridge Connect: self-hosted email sending with direct-to-MX
  delivery and no third-party provider.
- HTTP API (`POST /send` with batch personalizations, merge tags, idempotency keys, scheduling)
  and authenticated SMTP submission (587/465) with API keys.
- MTA: Postfix with DKIM signing from the database (two selectors), VERP return paths,
  RFC 8058 one-click `List-Unsubscribe`, per-client limits on the public ports, TLS certificate
  issued by Caddy.
- Tracking of opens, clicks and unsubscribes with bot and privacy-proxy filtering.
- Bounce processing (synchronous and asynchronous) and ARF complaint reports, both turning into
  suppressions.
- Webhooks for sent, delivered, deferred, bounced, opened, clicked, unsubscribed and complaint
  events, signed with HMAC-SHA256 and retried with exponential backoff.
- Deliverability jobs: per-domain volume governor, blocklist monitoring with node failover,
  health quarantine, SPF/DKIM/DMARC audits, delivery metrics. A new sending domain starts at
  500 emails a day (`bootstrap_tenant.py --daily-limit`); what is over the limit waits for the
  next day, and the API answers `429` with `Retry-After` when tomorrow is full as well.
- `make demo`: a demo store with real sends through the pipeline and synthetic history.
- `/start-here` skill for Claude Code: a guided local trial or server install.
- Control panel (English and Portuguese): overview with rates, emails and their journey,
  domains with their DNS records, API keys, suppressions, webhooks.
- Prometheus metrics at `/metrics` on the internal network.
- Single-VM deployment with Docker Compose, per-service secrets, isolated internal networks,
  `scripts/deploy.sh` for a fresh VPS and `scripts/preflight.sh` for port 25, PTR and blocklists.
- End-to-end test suites against a fresh local stack and a load test (`make loadtest`).

## Roadmap

- `make node-add`: provision an additional sending node on a fresh VPS over SSH, filling the
  node pool state that per-IP warm-up routing needs.
- Local recursive resolver (unbound) for blocklist queries.
- Encrypted DKIM private keys at rest.
- High availability for the control node.
