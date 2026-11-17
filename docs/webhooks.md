# Webhooks

A webhook is a URL that receives a JSON `POST` for every event of a sending domain. Create
them in the panel (*Webhooks*) or with the API.

## Creating one

```http
POST /webhooks
Authorization: Bearer <api key with write:webhook, or a panel session>
Content-Type: application/json

{
  "name": "orders",
  "url": "https://hooks.shop.example/connect",
  "events": ["delivered", "bounced", "complaint", "opened", "clicked", "unsubscribed"],
  "secret_key": "a-long-random-secret",
  "headers": {"X-Source": "connect"}
}
```

- The webhook belongs to the domain of the credential that creates it; it receives that
  domain's events (and its subdomains', such as the return-path domain).
- URLs that resolve to private, loopback or link-local addresses are rejected, on creation
  and on every redirect.
- `PATCH /webhooks/{id}/toggle` pauses and resumes it, `POST /webhooks/{id}/test` sends a test
  event, `GET /webhooks/{id}/logs` lists every attempt with the HTTP status and response.

## Events

| Event | When |
|---|---|
| `sent` | the MTA accepted the message for delivery |
| `delivered` | the recipient's server accepted it (`250`) |
| `deferred` | a temporary failure; Postfix will retry |
| `bounced`, `hard_bounced`, `soft_bounced` | a permanent or temporary rejection, synchronous or asynchronous. Subscribing to `bounced` delivers every bounce; the payload's `event_type` says `hard_bounced` or `soft_bounced` |
| `opened` | the open pixel was loaded by a person (scanners and privacy proxies filtered) |
| `clicked` | a tracked link was followed (`click` is accepted as an alias) |
| `unsubscribed` | the recipient used the unsubscribe link or one-click `List-Unsubscribe` |
| `complaint` | a mailbox provider's feedback loop reported the message as spam |
| `test` | sent by `POST /webhooks/{id}/test` |

## Request

```http
POST /connect HTTP/1.1
Host: hooks.shop.example
Content-Type: application/json
User-Agent: RevBridge-Webhook/1.0
X-Webhook-Event: delivered
X-Webhook-Message-Id: 4f6c2b3e-…
X-Webhook-Timestamp: 1762603200
X-Webhook-Signature: sha256=5d1f…
X-Source: connect
```

```json
{
  "event_type": "delivered",
  "message_id": "4f6c2b3e-…",
  "timestamp": "2026-11-08T12:00:03.120000",
  "service_type": "email",
  "recipient_email": "ana@example.org",
  "domain_name": "shop.example",
  "domain_id": 7,
  "delivery_time": "2026-11-08T12:00:03",
  "server_response": "250 2.0.0 OK",
  "message_headers": {"Subject": "Order 1234 confirmed", "From": "orders@shop.example"},
  "data": {}
}
```

Event-specific fields:

| Event | Fields |
|---|---|
| `sent` | `subject`, `sender_email`, `recipient_count` |
| `delivered` | `delivery_time`, `server_response` |
| `bounced` (and variants) | `bounce_type` (`hard`, `soft`…), `bounce_reason`, `bounce_code`, `server_response` |
| `opened` | `user_agent`, `ip_address` |
| `clicked` | `clicked_url`, `user_agent`, `ip_address` |
| `complaint` | `complaint_type`, `complaint_feedback`, `source_ip` |
| `unsubscribed` | `unsubscribe_reason`, `unsubscribe_url` |

## Verifying the signature

`X-Webhook-Signature` is `sha256=` followed by the hex HMAC-SHA256 of the **raw request body**
with the webhook's `secret_key`. Compute it over the bytes you received, before parsing the
JSON, and compare in constant time.

Python:

```python
import hashlib, hmac

def valid(body: bytes, header: str, secret: str) -> bool:
    expected = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, header or "")
```

Node.js:

```js
const crypto = require("crypto");

function valid(rawBody, header, secret) {
  const expected = "sha256=" + crypto.createHmac("sha256", secret).update(rawBody).digest("hex");
  const a = Buffer.from(expected), b = Buffer.from(header || "");
  return a.length === b.length && crypto.timingSafeEqual(a, b);
}
```

Reject requests whose `X-Webhook-Timestamp` is more than a few minutes old to make replays
useless. The body is identical across retries of the same event, so the signature is too.

## Delivery and retries

- Success is any HTTP status below 400 within the webhook's timeout (30 s by default).
- `5xx`, timeouts and network errors are retried with exponential backoff (5 s, 10 s, 20 s…,
  capped at 5 minutes) up to the webhook's retry count (3 by default).
- `4xx` is not retried: fix the endpoint and use the logs to see what was rejected.
- Events are delivered at least once and may arrive out of order; use `message_id`,
  `event_type` and `timestamp` to de-duplicate and order them.
