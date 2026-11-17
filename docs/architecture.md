# Architecture

Connect is a set of small services around Postfix, all started by one `docker-compose.yml`.

## Services

| Service | Image | What it does |
|---|---|---|
| `api` | app | HTTP API: `POST /send`, domains, DKIM, API keys, suppressions, webhooks, reports, panel login |
| `queue-processor` (×N) | app | pulls messages from the Redis queue and submits them to the MTA |
| `ledger-reaper` | app | closes sends the ledger says were lost: deadletter, or delivered when a delivery event confirms them |
| `webhook-dispatcher` | app | delivers events to customer webhooks (signed, with retries) |
| `tracking` | app | open pixel, click redirect and unsubscribe (including RFC 8058 one-click POST) |
| `events` | app | ingests delivery events from the MTA log, asynchronous bounces and complaint reports |
| `scheduler` | app | runs the deliverability jobs listed in `connect.toml` |
| `milter` | milter | rewrites links, adds the open pixel, `List-Unsubscribe`, Message-ID and the VERP return path |
| `mta` | mta | Postfix (submission, MX, delivery), OpenDKIM (signing), Dovecot (SASL only) |
| `vector` | vector | tails the Postfix log and posts structured delivery events to `events` |
| `image-proxy` | image-proxy | serves images with a solid background behind transparent PNGs, converts SVG |
| `caddy` | caddy | TLS (Let's Encrypt), host routing, the control panel |
| `db` / `redis` / `redis-cache` | upstream | MySQL 8.4, Redis 7 (queue, persistent), Redis 7 (image cache, evicting) |

## Sending path

1. `POST /send` authenticates the API key, checks that the sender belongs to the caller,
   removes suppressed recipients, checks the per-domain daily quota (warm-up), writes one row
   per recipient in the **send ledger** (`queued`) and pushes the message to the customer's
   queue in Redis. Fairness tokens give every customer a turn, so one big campaign cannot
   starve everyone else.
2. A queue processor claims the ledger row atomically (`sending`) and submits the message to
   the MTA's internal submission port with SMTP AUTH, using the customer's API key.
3. Postfix runs the **milter** (tracking rewrite, headers, VERP) and **OpenDKIM** (signature
   with the domain's key from the database), then accepts the message (`smtp_accepted`).
4. Postfix routes it: by recipient provider (`recipient_relays`), by sending zone
   (`zone_relays`), otherwise straight to the recipient's MX from the control node.
5. Vector reads `status=sent` / `bounced` / `deferred` from the Postfix log and posts it to
   `events`, which updates `deliveries`, `delivery_events` and the ledger (`delivered`).

The ledger makes sending at-least-once with de-duplication: a crashed worker's lease
expires and the reaper re-queues the message; a message accepted by the MTA is never
submitted twice.

## Event path

Every state change (sent, delivered, deferred, bounced, opened, clicked, unsubscribed,
complained) is written to the `kafka_outbox` table in the same transaction as the state
itself. The webhook dispatcher drains that table, matches the event against the active
webhooks of the customer's domain and POSTs it (see [Webhooks](webhooks.md)).

## Tracking path

The milter rewrites each link to `https://<tracking>/c/<token>` and adds a pixel
`https://<tracking>/o/<token>.png`. Tokens are Fernet-encrypted (message, recipient, URL), so
they cannot be forged or edited. The tracking service classifies the request (real person,
link scanner, Apple Mail Privacy Protection) before recording an open or a click, and
redirects clicks to the original URL.

## Bounces and complaints

- **Synchronous** bounces (a 5xx during delivery) come from the Postfix log.
- **Asynchronous** bounces (a DSN sent later) arrive at the MX on port 25, addressed to the
  VERP return path `bounces+<recipient>@rbm<n>.<domain>`; Postfix pipes them to `events`,
  which checks that they refer to a real send in the ledger, classifies them and suppresses
  hard bounces.
- **Complaints** (ARF reports from feedback loops) arrive at `bounces+fbl@rbm<n>.<domain>`.
  A report is accepted only if it refers to a message in the send ledger; the recipient is
  suppressed with reason `complaint`.

## Storage

| Store | Holds |
|---|---|
| MySQL | customers, domains, DKIM keys, API keys, users, email configs, send ledger, deliveries and events, suppressions, webhooks and their attempts, nodes and routing, job state |
| Redis (`redis`) | the send queue (AOF, no eviction), caches, idempotency keys, rate limits, metrics |
| Redis (`redis-cache`) | processed images only; bounded memory, LRU eviction |
| Volumes | MySQL data, Redis AOF, Postfix spool, MTA logs, Caddy certificates |

## Networks

| Network | Internet | Members |
|---|---|---|
| `edge` | yes | caddy, api, tracking, webhook-dispatcher, scheduler, mta, image-proxy |
| `data` | no (internal) | db, redis and every service that uses them |
| `cache` | no (internal) | image-proxy, redis-cache |

The image proxy, which fetches and decodes untrusted images, is not on the `data` network: it
cannot reach the database or the queue. See [Security](security.md).

## Multi-tenancy

A **customer** (client id) owns sending domains, users and API keys. Every API route scopes
reads and writes to the customer of the credential: an id taken from a request body or query
string is only accepted when it belongs to that customer. Each domain has its own DKIM keys,
quota and suppression list; the webhook dispatcher only delivers a domain's events to that
domain's webhooks.
