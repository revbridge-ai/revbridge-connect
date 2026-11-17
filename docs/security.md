# Security

This page describes how a Connect installation is hardened and what you are responsible for.
To report a vulnerability, follow [SECURITY.md](../SECURITY.md).

## Your responsibilities

- Keep the VM patched and SSH restricted to keys.
- Keep `secrets/` private and backed up (mode 0600, never committed). Anyone with these files
  can read the database and sign tracking links.
- Only open the ports Connect needs: 25, 80, 443, 465, 587 (and 22 for you). Docker publishes
  only those.
- Rotate API keys you suspect were exposed (panel, *API keys*).

## Isolation between customers

- Every API route scopes reads and writes to the customer of the credential. A `client_id` or
  `domain_id` in a request body or query string is only accepted when it belongs to that
  customer; anything else returns 403/404.
- A sender address on a domain registered to another customer cannot be approved.
- DKIM keys, DKIM identities, email configurations, webhooks, suppressions and statistics are
  visible only to the owning customer.
- Only panel admins, or API keys with the `admin` permission, can create, change or revoke API
  keys, and permissions can only be granted within the same customer.
- The webhook dispatcher delivers a domain's events only to that domain's webhooks.

## Credentials and sessions

- API keys are 256-bit random secrets stored as SHA-512-crypt hashes; the plain key is shown once.
- Panel sessions are JWTs signed with a generated secret and always expire (12 hours by default).
  There is no default secret: without `JWT_SECRET_KEY` sessions are refused.
- There is no authentication bypass switch and no trusted "internal service" header: every
  request is authenticated.

## Secrets and networks

- `make env` generates every secret; nothing has a default value in the code.
- Each container receives only the secrets of its scope (see
  [Configuration](configuration.md#secrets-and-environment-files)). The MySQL root password
  only reaches the `db` container.
- MySQL and Redis are on an internal network with no route to the internet. The image proxy,
  which downloads and decodes untrusted images, is not on that network at all.
- The MTA uses a dedicated database user with `SELECT` on the few tables it reads.
- Containers run as unprivileged users with all Linux capabilities dropped and
  `no-new-privileges`, except the MTA, MySQL and Redis, which need to switch users at start.

## SMTP

- Port 25 is a receive-only MX (bounces and complaint reports). It never accepts
  authentication and only relays to the installation's own return-path domains.
- Submission (587 with STARTTLS, 465 with TLS) requires authentication with an API key that has
  `write:email`. If the milter is unavailable the message is refused with a temporary error
  instead of leaving without tracking and checks.
- The public ports have per-client limits on connections, messages and authentication attempts.
- The queue submits through an internal port that is not published on the host.
- TLS uses the certificate Caddy obtains from Let's Encrypt for the submission host, reloaded on
  renewal.

## HTTP

- Caddy terminates TLS for every public host.
- `/metrics` is not reachable from the internet.
- The panel is served with `X-Frame-Options: DENY`, `X-Content-Type-Options: nosniff` and a
  strict referrer policy.

## Links, images and webhooks

- Tracking tokens (open, click, unsubscribe) are Fernet-encrypted: a token cannot be forged or
  edited, so the click endpoint only redirects to URLs that were in your emails. Tokens are never
  accepted in clear text.
- Image proxy URLs are signed with HMAC-SHA256; the proxy refuses unsigned URLs, caps download
  size and pixel count, refuses private and internal addresses (on every redirect too), and
  renders SVG without loading any external resource. Its cache is a separate, size-bounded Redis.
- Webhook URLs that resolve to private, loopback or link-local addresses are refused, including
  after redirects. Every webhook request is signed (see [Webhooks](webhooks.md)).
- Complaint reports are accepted only for messages that are in the send ledger. Asynchronous
  bounces too: the DSN must quote a message from the ledger for that recipient or, when the
  remote server does not return the original headers, the domain must have mailed that
  recipient recently. A forged bounce cannot suppress your recipients.
- Idempotency keys are scoped to the customer that sends them.

## Known limitations

Things we know about and have chosen to document rather than hide. None is reachable from the
internet in the default setup.

- **The queue holds the caller's API key until the message is sent.** The worker authenticates
  to the internal submission port with it. Redis requires a password and is on an internal
  network, but anyone who can read Redis (or its `appendonly` file) can read keys of messages
  still queued. Keep `secrets/` private and do not publish port 6379.
- **The events-updater and the milter ports are not authenticated.** Ports 8081 and 8901 are
  only on the internal Docker network. Never publish them or route them through a proxy.
- **Revoked API keys keep working for up to `API_KEY_CACHE_TTL` seconds** (30 by default; up to
  twice that in the worst case) because validation results are cached.
- **DKIM private keys, SMTP passwords and webhook secrets are stored in clear text in MySQL**
  and cached in Redis. Database encryption at rest is up to your disk or volume.
- **Webhook URLs are checked once for private addresses and again on every redirect, but DNS is
  resolved separately for the request**, so a hostile DNS server could still answer differently
  (DNS rebinding). Keep the API container's egress restricted if this matters to you.
- **Domains are registered by the operator** (`scripts/bootstrap_tenant.py`), never through the
  API. Do not register a domain for one customer and its parent or subdomain for another: the
  suppression and webhook scoping match a domain together with its subdomains.

## Logs

Container logs rotate (20 MB × 5 files per container). The MTA rotates `mail.log` at 100 MB.
Logs contain recipient addresses: treat them as personal data.
