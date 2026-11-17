# Installation

This puts the control node on the internet: one VPS that runs every service and also sends.

## 1. Choose a server

| Need | Why |
|---|---|
| Public IPv4 that is yours alone | your reputation is the reputation of this IP |
| Outbound TCP 25 open | many clouds block it by default (AWS, GCP, Azure); ask support or pick a provider that allows it |
| A reverse DNS (PTR) you can set | receivers reject mail from IPs without forward-confirmed reverse DNS |
| 4 vCPU, 8 GB RAM, 80 GB SSD | comfortable for millions of emails per month on one node (see [Scaling](scaling.md)) |
| Ubuntu 22.04/24.04 or Debian 12 | what `scripts/install.sh` supports |

Check the IP before you commit to it: it should not be on [Spamhaus](https://check.spamhaus.org/)
or other major blocklists. `scripts/preflight.sh` checks this for you after the install.

### Which providers allow outbound port 25

Port 25 policy is the provider's, it changes, and it can differ by account. Treat this table as
a starting point and run `scripts/check-vps.sh root@<ip>` before you pay for a month: it tests
outbound port 25 without changing anything.

| Provider | Port 25 | Source |
|---|---|---|
| **Contabo** | open, no ticket | **Proven by us.** RevBridge sends from Contabo VPSs and has delivered more than 1 million emails through them. |
| **Velia (Private Cloud)** | open | **Confirmed by the RevBridge team.** We have not measured volume on it the way we have on Contabo. |
| Vultr | blocked by default; opened after a support ticket | provider policy, reported by several guides |
| DigitalOcean | blocked on new accounts; opened after a support ticket | provider policy, reported by several guides |
| AWS (EC2), Google Cloud, Azure, Oracle Cloud | blocked; each has its own request process | provider policy |
| Hetzner, OVHcloud, Linode | reports conflict (open by default, or open on request) | **not verified by us**: ask support first |

Only the Contabo and Velia rows come from our own use; the others are what providers and
public guides say today. If you verify another provider, tell us in
[Discussions](https://github.com/revbridge-ai/revbridge-connect/discussions) or open a pull
request to add it.

## The short way

```bash
scripts/check-vps.sh root@IP                                   # read-only look at the server
scripts/init_manifest.py --ip IP --domain acme.com --email you@acme.com   # manifest outside the checkout
scripts/deploy.sh root@IP
scripts/first_customer.sh root@IP --domain acme.com --customer "Acme" --admin-email you@acme.com
scripts/go_live.sh root@IP
```

The scripts use the newest manifest `init_manifest.py` wrote (set `CONNECT_TOML` to choose another).
`--domain` is the domain your email is sent from. The installation gets its own subdomain
(`mail.acme.com`) with the hostnames below. Set the IP's reverse DNS in your VPS panel while the
deploy builds. `first_customer.sh` prints every DNS record in one list (steps 5 and 6 explain
them); the rest of this page is the reference for what each command does.

## 2. Plan the names

The installation uses a domain you control (`mail.acme.com` by default; a dedicated
`acme-mail.com` also works). Connect uses one hostname per role, all set in `connect.toml`:

| `connect.toml` | Example | Points to | Used for |
|---|---|---|---|
| `install.hostname` | `mta1.example.com` | the VPS IP (A + PTR) | HELO name, the IP's reverse DNS |
| `endpoints.api` | `api.example.com` | the VPS IP | `POST /send` and the reports API |
| `endpoints.tracking` | `t.example.com` | the VPS IP | open pixel, click and unsubscribe links |
| `endpoints.submission` | `smtp.example.com` | the VPS IP | SMTP submission 587/465 (its TLS certificate is issued by Caddy) |
| `endpoints.bounce` | `bounce.example.com` | the VPS IP | MX for asynchronous bounces |
| `endpoints.images` | `img.example.com` | the VPS IP | image proxy |
| `endpoints.panel` | `panel.example.com` | the VPS IP | control panel |

The A records are published together with all the others after the first customer exists
(step 5); only the PTR of the IP, set to `install.hostname` in your VPS provider's panel, is
worth doing right away.

## 3. Configure

`scripts/init_manifest.py` writes this for you, outside the checkout. By hand, edit `connect.toml`:

```toml
[install]
domain    = "example.com"
hostname  = "mta1.example.com"   # = PTR of public_ip
public_ip = "203.0.113.10"
admin_email = "postmaster@example.com"   # Let's Encrypt account and DMARC reports

[endpoints]
api        = "api.example.com"
tracking   = "t.example.com"
submission = "smtp.example.com"
bounce     = "bounce.example.com"
images     = "img.example.com"
panel      = "panel.example.com"
```

Everything else has working defaults. [Configuration](configuration.md) explains each section.

## 4. Deploy

From your machine, with SSH access to the VPS as root (or a sudoer):

```bash
scripts/deploy.sh root@203.0.113.10
# or with a specific key: SSH_KEY=~/.ssh/my_vps scripts/deploy.sh root@203.0.113.10
```

`deploy.sh` copies the repository to `/opt/revbridge-connect` (never your local `.env` or
`secrets/`) and runs `scripts/install.sh` on the server, which:

1. installs Docker Engine, `ufw` and a few tools;
2. sets the hostname and opens only 22, 25, 80, 443, 465 and 587;
3. runs the preflight (outbound 25, PTR, blocklists);
4. generates the secrets **on the server** (`make env`) — they never leave it;
5. builds the images and starts the stack.

The install runs on the server in the background and `deploy.sh` follows its log
(`/var/log/connect-install.log`). If your connection drops, the install keeps going; run `deploy.sh`
again and it reattaches. The last line is `deploy finished: exit N`; only `0` is a success.

Caddy obtains the Let's Encrypt certificates for every endpoint, including the submission host,
whose certificate the MTA picks up automatically. It starts only once the endpoint names resolve to
the VPS (Let's Encrypt limits failed attempts): if they do not yet, the installer starts everything
else and `scripts/go_live.sh` starts Caddy after you publish the DNS records.

The first install builds the images on the server (5 to 10 minutes). With prebuilt images
(`CONNECT_IMAGE_REGISTRY=ghcr.io/ORG CONNECT_VERSION=1.0.0 scripts/deploy.sh ...`, published by the
`Release images` workflow on each `v*` tag) it pulls them instead; a missing image falls back to the build.

## 5. Create the first customer

```bash
scripts/first_customer.sh root@203.0.113.10 --domain shop.example --customer "Example Shop" \
  --admin-email you@shop.example
```

It runs `scripts/bootstrap_tenant.py` on the server and saves the output (the API key, shown once,
and the panel password) in `~/.config/revbridge-connect/bootstrap-shop.example.json`, readable only
by you. It then prints every DNS record in one table (see below). The bootstrap has two groups of
DNS records:

- **Installation zone** (once per installation): an A, an MX and an SPF record for the zone
  host, plus the two DKIM public keys (`s1`, `s2`) and the PTR reminder.
- **Customer domain** (once per sending domain): two DKIM CNAMEs, a CNAME for the return-path
  (VERP) subdomain `rbm<n>.<domain>` and a DMARC record. Because the customer only creates
  CNAMEs, keys can rotate and nodes can change without touching the customer's DNS again.

The panel's **Domains** page shows the same records with their current status.

`scripts/dns_records.py` (standard library Python, needs `dig`) turns them into one list and helps
publish them:

- `plan` finds the zone of your domain, says who hosts it (Cloudflare, Route 53, GoDaddy and others
  are recognized by their name servers), prints all records and writes a zone file you can import.
- `apply` creates them at **Cloudflare** with a token you make (permissions *Zone > DNS > Edit* on
  that zone only), read from the `CLOUDFLARE_API_TOKEN` variable of your own shell. It is a dry run
  until you pass `--apply`, creates records DNS-only (never proxied) and never changes or deletes
  one that exists: a conflict is listed and left for you.
- `check` asks the zone's own name servers and a public resolver whether each record is there.

The PTR cannot be automated: your VPS provider's panel is the only place to set it.

## 6. Verify

```bash
scripts/go_live.sh root@203.0.113.10
```

It checks the DNS records, starts Caddy (the certificates) and runs the full preflight on the
server (`bash scripts/preflight.sh`). It must report outbound 25 open, PTR = hostname (forward-confirmed), the IP clean on the
blocklists and, once DNS has propagated, SPF, MX and DKIM for the zone.

Send a first message to a few seed inboxes you own (Gmail, Outlook, Yahoo) and check the
headers: `spf=pass`, `dkim=pass` and `dmarc=pass`.

## 7. Warm up

A new IP has no reputation. Connect starts each new sending domain at 500 emails a day and
raises the limit about 30% for each day of clean delivery (see
[Deliverability](deliverability.md#warm-up)); what is over the limit waits for the next day.
The per-IP warm-up is not active yet, so on a new IP also keep the first weeks small and send
your most engaged recipients first. Register
the feedback-loop address printed by the bootstrap (`bounces+fbl@rbm<n>.<domain>`) with the
mailbox providers' complaint programs (Yahoo, Microsoft SNDS/JMRP) so complaints become
suppressions.

## Updating

Run `scripts/deploy.sh` again. It is idempotent: it syncs the code, keeps the generated
secrets and volumes, rebuilds what changed and restarts the affected containers.
