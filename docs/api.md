# HTTP API

Base URL: `https://<endpoints.api>` in production, `http://localhost:8001` locally. The full
contract is the OpenAPI document in [openapi/mail-api.json](openapi/mail-api.json); the running
API also serves interactive docs at `/docs`.

## Authentication

Every request carries `Authorization: Bearer <token>`. Two kinds of token exist:

| Token | Who | How to get one |
|---|---|---|
| **API key** | applications | the bootstrap prints the first one; more in the panel (*API keys*) or `POST /api-keys` |
| **Session (JWT)** | people using the panel | `POST /auth/login` with email and password; valid for 12 hours |

API keys carry **permissions**. The ones that matter:

| Permission | Allows |
|---|---|
| `write:email` | send (`POST /send`) and SMTP submission |
| `read:email` | status, messages, email configs |
| `read:stat` | reports |
| `read:webhook` / `write:webhook` | list / manage webhooks |
| `read:suppression` / `write:suppression` | list / manage suppressions |
| `admin` | everything, including managing API keys |

Only panel admins and keys with `admin` can create, change or revoke API keys. A key is always
bound to one customer, and every route only sees that customer's data.

The same API key works as the SMTP password: connect to `endpoints.submission` on 587
(STARTTLS) or 465 (TLS) with the API key id (`ak_…`) as the user name and the key as the
password.

## Sending

```http
POST /send
Authorization: Bearer <api key with write:email>
Idempotency-Key: order-1234-confirmation        (optional, recommended)
Content-Type: application/json
```

```json
{
  "personalizations": [
    {
      "to": [{"name": "Ana", "address": "ana@example.org"}],
      "substitutions": {"first_name": "Ana", "order": "1234"},
      "headers": {"X-Order": "1234"}
    }
  ],
  "from": {"name": "Example Shop", "address": "orders@shop.example"},
  "reply_to": {"address": "help@shop.example"},
  "subject": "Order {{order}} confirmed",
  "content": [
    {"type": "text/plain", "value": "Hi {{first_name}}, your order {{order}} is confirmed."},
    {"type": "text/html", "value": "<p>Hi {{first_name}}, your order <b>{{order}}</b> is confirmed.</p>"}
  ],
  "headers": {"X-Campaign": "transactional"},
  "scheduled_at": null
}
```

- Up to many personalizations per request; each one is a separate message to its `to`
  recipients, with its own substitutions. Batching (for example 50–100 recipients per request)
  is the fastest way to send a campaign.
- `from.address` must belong to one of the caller's domains (or an approved sender).
- Suppressed recipients are dropped before anything is queued; the response says how many were
  queued (`recipients_count`).
- `scheduled_at` (ISO 8601) delays the send.
- Each sending domain has a daily limit that grows as it warms up
  ([Deliverability](deliverability.md#warm-up)). What is over today's limit is scheduled for
  tomorrow and reported in `scheduled_count`; when tomorrow is full as well, the request gets a
  `429` with `Retry-After` and nothing is queued.
- With the same `Idempotency-Key` the API returns the first result instead of sending again.
  Keys are scoped to the customer.

Response (`202`/`200`):

```json
{"message_id": "4f6c…", "status": "queued", "domain_name": "shop.example",
 "queue": "e:queue", "timestamp": "2026-11-08T12:00:00Z", "recipients_count": 1}
```

## Following a message

| Route | Returns |
|---|---|
| `GET /status/{message_id}` | current state per recipient: queued, sent, delivered, deferred, bounced, failed |
| `GET /stats/messages` | paginated list of messages with their last state (the panel's *Emails*) |
| `GET /stats/message/headers?message_id=…` | the headers the message was sent with |
| `GET /stats/dashboard` | sent, delivered, opened, clicked, bounced, complained, unsubscribed, with rates |
| `GET /deliverability/` | delivery, bounce and deferral rates over time |

## Suppressions

| Route | |
|---|---|
| `GET /suppressions` | list (filter by type: `hard`, `soft`, `complaint`, `unsubscribe`, `manual`) |
| `POST /suppressions` | add addresses manually |
| `DELETE /suppressions/{id}` | remove one (for example after a recipient asks to be mailed again) |

## Domains and DKIM

| Route | |
|---|---|
| `GET /domains/{domain}/dns-info` | the DNS records the domain must publish, and the feedback-loop address |
| `GET /dkim/keys` | the domain's DKIM keys and the TXT values to publish |
| `GET /email-configs` | per-domain sending configuration (tracking on/off, envelope prefix) |
| `/approved-senders` | addresses outside your domains that may appear in `From` |
| `/authorized-domains/{api_key_id}` | extra domains (of the same customer) a key may send from |

New sending domains are created with `scripts/bootstrap_tenant.py` (it creates the DKIM keys
and the return-path domain together).

## Webhooks

`/webhooks` creates, lists, updates, toggles, tests and deletes webhooks; `/webhooks/{id}/logs`
shows every delivery attempt. See [Webhooks](webhooks.md).

## Errors

| Status | Meaning |
|---|---|
| 400 / 422 | invalid request (the body explains which field) |
| 401 | missing or invalid token |
| 403 | the token lacks the permission, or the resource belongs to another customer |
| 404 | not found (also used for another customer's resources) |
| 409 | conflict (for example an idempotency key still in flight) |
| 429 | rate limited, or the domain's daily limit is used up for today and tomorrow (`Retry-After` says when to try again) |

## Metrics

`GET /metrics` (Prometheus text format) is only reachable from the internal network; Caddy
returns 404 for it on the public hosts. It exposes queue depth, the ledger by state, job runs,
node state, delivery series and webhook dispatcher counters.
