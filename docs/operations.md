# Operations

All commands run in `/opt/revbridge-connect` on the control node.

## Everyday commands

| | |
|---|---|
| `docker compose ps` | state and health of every container |
| `docker compose logs -f --tail=100 <service>` | logs of one service (`api`, `queue-processor`, `mta`, …) |
| `docker compose exec mta postqueue -p` | the MTA queue (deferred messages and why) |
| `docker compose exec mta postqueue -f` | retry the MTA queue now |
| `make jobs` | the deliverability jobs and their schedule |
| `bash scripts/preflight.sh` | port 25, PTR, blocklists, DNS of the zone |

## Backups

Back up three things together, at least daily:

1. **MySQL**:
   ```bash
   docker compose exec -T db sh -c 'exec mysqldump -uroot -p"$MYSQL_ROOT_PASSWORD" \
     --single-transaction --routines --events "$MYSQL_DATABASE"' | gzip > connect-$(date +%F).sql.gz
   ```
2. **Secrets**: the `secrets/` directory (without `TOKEN_ENCRYPTION_KEY`, links in emails
   already sent stop working; without the DKIM keys in the database, nothing can be signed).
3. **`connect.toml`** and any change you made to `config/variables.overrides.toml`.

Redis holds the send queue with AOF persistence (`redis-data` volume). Messages in the queue at
the moment of a crash are recovered from the AOF; the send ledger in MySQL lets the reaper
re-queue anything that was in flight.

Restore: start `db` with an empty volume, load the dump, put `secrets/` back, then `make up`.

## Upgrades

```bash
git pull                      # or scripts/deploy.sh from your machine
make env                      # picks up new variables; existing secrets are kept
docker compose up -d --build
```

Read [CHANGELOG.md](../CHANGELOG.md) first: schema changes are listed there. The initial schema
in `config/db/initdb/` only runs on an empty database volume.

## Metrics

`api:8001/metrics` serves Prometheus metrics on the internal network. Run Prometheus on the
same Docker network (or scrape through an SSH tunnel). Useful series:

| Metric | Watch for |
|---|---|
| `connect_queue_depth` | a queue that keeps growing: add queue processors or check the MTA |
| `connect_send_ledger_24h{state=…}` | `failed` / `deadletter` growing |
| `connect_job_last_run_timestamp_seconds{job=…}` | a job that stopped running |
| `connect_node_state` | a node marked as listed |
| `connect_suppressions_active` | sudden jumps (a bad list or a bounce storm) |
| `connect_webhooks_*` | webhook deliveries and failures (published by the dispatcher every minute) |

The panel's *Overview* shows the same delivery, bounce, complaint and unsubscribe rates.

## Troubleshooting

| Symptom | Look at |
|---|---|
| `POST /send` returns 403 | the `from` address is not on one of the key's domains, or the key lacks `write:email` |
| part of a send was scheduled for tomorrow | the domain reached its warm-up quota for today (`GET /volume/allowance`); the rest goes out the next day |
| `POST /send` returns 429 | more than 10× today's quota in one go, or more than `RATE_LIMIT_REQUESTS` requests per minute for the key |
| messages stay `queued` | `docker compose logs queue-processor`; is the `mta` container healthy? |
| messages stay `smtp_accepted` | Vector or `events` is not ingesting the MTA log (`docker compose logs vector events`) |
| many `deferred` | `postqueue -p` in the MTA; a provider is throttling a new IP (warm-up) |
| no opens or clicks | the tracking hostname must resolve and have a certificate; check `endpoints.tracking` |
| Gmail shows "via" or fails DMARC | the customer's CNAMEs for DKIM and the return-path are missing (*Domains* page) |
| TLS errors on 587 | Caddy has not obtained the certificate for `endpoints.submission` yet (`docker compose logs caddy`) |
| preflight or the MTA shows DNS "timed out" | one of the VPS provider's resolvers is not answering (seen on Contabo): set working resolvers in `/etc/systemd/resolved.conf.d/` (`DNS=…`, `Domains=~.`) and in `/etc/docker/daemon.json` (`"dns": […]`), then restart `systemd-resolved` and Docker. Prefer the provider's working resolver: Spamhaus refuses queries from large public resolvers |
