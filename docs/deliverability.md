# Deliverability

Sending is easy; landing in the inbox is the hard part. Connect ships the automation that a
team running its own mail servers needs, enabled by default.

## Authentication

Every message is signed with DKIM using the sending domain's key (two selectors, so a key can
rotate without downtime). The envelope sender is a VERP address on `rbm<n>.<domain>`, a
subdomain the customer delegates with a CNAME; it carries the SPF record of your nodes, so SPF
aligns with the From domain under relaxed alignment. Publish DMARC (`p=none` to start, then
`quarantine`/`reject` once reports are clean). `domain_auth_audit` checks SPF, DKIM, DMARC and
PTR every hour and the panel shows the result per domain.

## Warm-up

Mailbox providers judge new IPs and new domains by their first weeks of traffic.

- **Per sending domain** (`volume_ramp`): each domain has a daily quota that grows 1.3× a day
  when the domain uses it with healthy rates, brakes when bounces or blocks rise and decays when
  the domain goes idle. A domain created with `scripts/bootstrap_tenant.py` starts at **500
  emails a day** (`--daily-limit` changes it), so even its first campaign is paced. A domain
  that sent before Connect knew it enters the governor when the hourly job sees its traffic,
  with a first quota of 1.2× its busiest recent day. `POST /send` enforces the quota without
  dropping anything: what is above today's quota is scheduled for tomorrow while tomorrow's
  quota has room. When tomorrow is full too, or a single request is more than 10× the quota,
  the API answers `429` with a `Retry-After` header and the caller sends the rest later.
  Purely transactional domains can be marked exempt.
- **Per IP** (`ip_warmup`), for installations with several sending nodes: the controller keeps
  a daily cap for each new node IP that grows while its bounce and block rates stay low. Routing
  a share of the traffic to warming IPs relies on the node pool state that node provisioning
  will fill ([roadmap](../CHANGELOG.md#roadmap)), so it is not active yet.

On a new IP, warm up by hand: start with a few hundred emails a day to your most engaged
recipients and roughly double while bounces and complaints stay low. `GET /volume/allowance`
and `GET /volume/health` show the governor's numbers.

## Bounces

- **Hard bounces** (unknown user, domain does not exist) suppress the address immediately.
- **Soft bounces** (mailbox full, temporary policy) are retried by Postfix and suppressed only
  after repeated failures.
- **Asynchronous bounces** (DSNs that arrive later) reach the MX at `rbm<n>.<domain>` on port
  25 and are processed the same way.

## Complaints (feedback loops)

When a recipient clicks "this is spam", providers that run a feedback loop send an ARF report.
Register `bounces+fbl@rbm<n>.<domain>` (printed by the bootstrap and shown on the panel's
*Domains* page) with:

- Yahoo / AOL: the Yahoo Complaint Feedback Loop;
- Microsoft (Outlook.com, Hotmail): JMRP and SNDS.

Gmail does not send per-message complaints; use Google Postmaster Tools for its spam rate. A
report is accepted only if it matches a message in the send ledger. The recipient is
suppressed with type `complaint`, a `complaint` webhook is sent and the panel's complaint rate
counts it. Keep that rate under 0.1%.

## Unsubscribe

Every message carries `List-Unsubscribe` with an HTTPS URL, plus
`List-Unsubscribe-Post: List-Unsubscribe=One-Click` (RFC 8058), which Gmail and Yahoo require
from bulk senders. Both headers are covered by the DKIM signature, which that RFC requires too
(`SignHeaders` in `mta/templates/opendkim/opendkim.conf`). The tracking service handles the
one-click POST and the link in the body; both suppress the recipient for that domain and emit an
`unsubscribed` event. There is no `mailto:` alternative yet.

Gmail shows its own "Unsubscribe" button next to the sender only when it considers the sender a
bulk sender, which depends on volume and reputation: a new domain with a few test emails may not
get it even with every header right. Put the unsubscribe link (`{{unsub_link}}`) in the body of
campaigns anyway.

Every message also carries a `Feedback-ID` (`<campaign>:mail:connect:<From domain>`, signed).
Register the sending domain in [Google Postmaster Tools](https://postmaster.google.com) to see
Gmail's spam-complaint rate per campaign.

## Suppressions

A suppressed address is removed from every send of that domain before anything is queued. Types:
`hard`, `soft`, `complaint`, `unsubscribe` and `manual`. Manage them in the panel or with
`/suppressions`.

## Honest metrics

Opens and clicks are filtered before they are counted: corporate link scanners that fetch every
URL, and Apple Mail Privacy Protection, which loads every pixel, are recognised and recorded as
machine activity, not as people. Treat open rates as a trend and clicks as the signal.

## Scheduled jobs

The `scheduler` service runs these jobs (`connect.toml`, `[deliverability.jobs.*]`):

| Job | Every | What it does |
|---|---|---|
| `domain_auth_audit` | hour | SPF, DKIM, DMARC and PTR of each domain |
| `delivery_stats` | 5 min | delivery series per sender and node group, exported to `/metrics` |
| `group_health` | 15 min | bounce, deferral and ceiling per node group; quarantines an unhealthy group |
| `volume_ramp` | hour | the per-domain daily quota (see Warm-up) |
| `ip_warmup` | hour | the per-IP ramp |
| `warmup_spf_authz` | 6 h | which node IPs each domain's SPF authorizes |
| `node_health` | 5 min | blocklist checks of every node IP; quarantine and automatic failover |
| `recipient_failover` | 10 min | a mailbox provider rejecting one node is routed to another node |
| `warmup_controller` | 20 min | per-node budget; throttles a node or moves traffic to one with room |
| `tier_controller` | hour | moves a high-volume zone to a dedicated node and back |
| `fleet_stats` | 5 min | node fleet metrics for `/metrics` |

`make jobs` lists them; `/metrics` exposes the last run, duration, result and failures of each.
