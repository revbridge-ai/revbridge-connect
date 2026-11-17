# Scaling

Two different limits decide how fast Connect sends:

1. **Pipeline throughput**: how many messages per second the API accepts and the queue
   processors hand to the MTA (DKIM-signed, tracked, logged). This is what `[scale]` in
   `connect.toml` controls and what `make loadtest` measures.
2. **Delivery reputation**: how many messages per day the mailbox providers accept from your
   IPs without throttling or junking them. This grows with warm-up and with the number of
   sending IPs, never with CPU.

## Measured throughput

`make loadtest` sends N messages through the full local stack (API, queue, queue processors,
milter, OpenDKIM, Postfix, Vector, event ingestion, send ledger, webhooks) and reads the send
ledger to time each stage. The MTA delivers to Mailpit, so remote MX latency is not part of
the number. Batches of 50 recipients per request, 16 concurrent requests.

Machine: Docker Desktop on a 10-core laptop with 7.6 GB for Docker (one VM for everything).

| `queue_processors` | messages | API accepted / s | delivered / s (average) | delivered / s (best minute) |
|---|---|---|---|---|
| 8 | 10,000 | 684 | 48 | 52 |
| 12 | 15,000 | 467 | 64 | 68 |

- The API accepts bursts far faster than the queue drains them; the queue (Redis, persistent)
  absorbs the difference, so a campaign burst of hundreds of messages per second is accepted
  at once and delivered at the pipeline rate.
- Delivery grows almost linearly with `queue_processors`. Each processor is a sequential
  sender that spends most of its time waiting on the MTA and the database (10–20% of a CPU
  each), so adding processors is cheap until the database or the MTA saturates. At 12
  processors MySQL used about 1.7 CPUs and the MTA about 0.9.
- 48 messages per second is about 4 million per day; 64 per second about 5.5 million.

Run it on your own VM before sizing a campaign:

```bash
make loadtest                                           # 10,000 messages, defaults
bash tools/dev/loadtest.sh /tmp/connect-e2e/bootstrap.json 20000 50 16   # messages, batch, concurrency
```

It only runs against the local development stack (`make dev`), never against real recipients.

## The knobs that matter

| Setting | Where | Effect |
|---|---|---|
| `queue_processors` | `connect.toml [scale]` | parallel senders into the MTA; the main throughput knob (default 8) |
| `api_workers` | `connect.toml [scale]` | uvicorn workers of the API (default 2); raise for very bursty traffic |
| `events_workers` | `connect.toml [scale]` | workers ingesting delivery events (default 2) |
| `DB_POOL_ENABLED`, `DB_EXECUTOR_WORKERS` | variable catalog | pooled database connections per process (on by default) |
| `RATE_LIMIT_REQUESTS` | variable catalog | requests per minute per API key (default 6000) |
| MySQL `innodb-buffer-pool-size` | `MYSQL_BUFFER_POOL` in the environment of `docker compose` | give MySQL more memory on bigger VMs (default 512M) |

Two database settings made the biggest difference and are on by default: pooled connections
(a send touches the database about a dozen times) and no binary log (with the binary log on,
every commit waits for an fsync). If you need the binary log for replication, enable it and
expect lower throughput.

Apply a change with `make env && docker compose up -d`.

## Sizing a VM

| Volume | VM | `queue_processors` |
|---|---|---|
| up to ~1 million emails per day | 2–4 vCPU, 4–8 GB RAM | 4–8 |
| a few million per day, campaign bursts | 4–8 vCPU, 8–16 GB RAM, NVMe disk | 8–16 |

Disk speed matters more than CPU for MySQL; prefer NVMe. Watch `connect_queue_depth` on
`/metrics`: a queue that keeps growing during a campaign means you need more processors (or a
bigger VM if MySQL is the busy container in `docker stats`).

## Reputation is the real ceiling

Mailbox providers rate-limit and filter by IP and by sending domain. A brand-new IP starts at a
few hundred messages per day and, when its bounce and complaint rates stay low, ramps up over a
few weeks to tens of thousands per day (the `ip_warmup` and `volume_ramp` jobs do this for you).
Plan the number of sending IPs from your daily volume at the end of the warm-up, not from the
pipeline numbers above, and keep complaint rates under 0.1%.
