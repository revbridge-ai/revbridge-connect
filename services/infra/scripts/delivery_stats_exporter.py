#!/usr/bin/env python3
"""delivery_stats_exporter.py — email DELIVERY metrics (READ-ONLY).

Answers "how are deliveries doing?" per status, egress node, sender and customer. The series
are published through mail_infra.metrics_sink (CONNECT_METRICS_SINK=redis) and the API serves
them on GET /metrics in Prometheus format.

The source is `delivery_events` (the final hop recorded by the events updater): indexed by
`ts`, it matches `deliveries` and carries the host and the DSN.

ATTRIBUTION — read this before trusting the per-sender breakdown:
  `delivery_events.domain_name` holds the sender (`rbm<id>.<domain>`) only when the event was
  correlated with a send; otherwise it holds the RECIPIENT's domain (gmail.com, yahoo.com).
  Those events become `sender_domain:_unattributed` instead of polluting the per-sender
  breakdown (the `SUBSTRING(domain_name, LOCATE('.')+1)` used by group_health would turn
  gmail.com into a "com" bucket and yahoo.com.br into "com.br").
  `mail.delivery.attributed_ratio` exists to keep that gap VISIBLE.

Emits (tags: env, service:mail-delivery):

  closed window of DS_WINDOW_MIN (type=count, so summing over any time range is correct;
  the buckets are aligned, with no overlap and no gap):
    mail.delivery.events{status, egress, sender_domain}
    mail.delivery.dsn{dsn, status}          # bounced/deferred only
    mail.ledger.terminal{state, customer_id}
    mail.smtp.code{code}

  snapshot (gauge):
    mail.delivery.attributed_ratio          # % with a known sender (24 h)
    mail.ledger.backlog{state}              # non-terminal right now
    mail.ledger.stuck{kind}                 # stalled beyond the grace period
    mail.outbox.pending / mail.outbox.oldest_pending_sec
    mail.sender.{attempts_24h,delivery_rate,hard_bounce_rate,defer_rate}{sender_domain,relay}
    mail.group.{delivered_per_day,load_pct,free_zones}{group_key}

Env:
  DS_WINDOW_MIN      window of the counts in minutes (default 5 = the job's schedule)
  DS_SENDER_MIN_VOL  minimum 24 h volume to emit reputation metrics (default 50)
  DS_GROUP_DAILY_CAP daily cap per shared group (default 50000)
  DS_STUCK_GRACE_SEC "stalled" grace period in the ledger (default 7200)
  DS_DRY_RUN         true = print the series instead of publishing them
  ENVIRONMENT        env tag (default: dev, so an ad-hoc run is never tagged as production)
  DB_*               MySQL (same connection as mail_infra)
"""

from __future__ import annotations

import json
import logging
import os
import sys

os.environ.setdefault("OTEL_SDK_DISABLED", "true")

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

WINDOW_MIN = int(os.getenv("DS_WINDOW_MIN", "5"))
WINDOW_SEC = WINDOW_MIN * 60
SENDER_MIN_VOL = int(os.getenv("DS_SENDER_MIN_VOL", "50"))
GROUP_DAILY_CAP = int(os.getenv("DS_GROUP_DAILY_CAP", "50000"))
STUCK_GRACE_SEC = int(os.getenv("DS_STUCK_GRACE_SEC", "7200"))
DRY_RUN = os.getenv("DS_DRY_RUN", "false").lower() in ("1", "true", "yes", "on")
ENV = os.getenv("ENVIRONMENT") or "dev"

# delivery_events and virtual_domains use different collations; every JOIN on a domain name
# needs this, or MySQL aborts with "Illegal mix of collations".
COLL = "COLLATE utf8mb4_0900_ai_ci"

LOG = logging.getLogger("delivery_stats")


def _rate(n, d) -> float:
    return round(n / d, 4) if d else 0.0


def _pct(n, d) -> float:
    return round(100.0 * n / d, 2) if d else 0.0


class Series:
    """Collects the series and publishes them in one go."""

    def __init__(self, bucket_start: int) -> None:
        self.bucket_start = bucket_start
        self.items: list[dict] = []

    def _tags(self, extra: dict[str, str]) -> list[str]:
        tags = [f"env:{ENV}", "service:mail-delivery"]
        for k, v in extra.items():
            # an empty value would produce a bare "k:" tag; report it as "unknown".
            tags.append(f"{k}:{v if v not in (None, '') else 'unknown'}")
        return tags

    def count(self, metric: str, value: float, **tags: str) -> None:
        self.items.append({
            "metric": metric,
            "points": [[self.bucket_start, float(value)]],
            "type": "count",
            "interval": WINDOW_SEC,
            "tags": self._tags(tags),
        })

    def gauge(self, metric: str, value: float, **tags: str) -> None:
        self.items.append({
            "metric": metric,
            "points": [[self.bucket_start, float(value)]],
            "type": "gauge",
            "tags": self._tags(tags),
        })

    def submit(self) -> None:
        if not self.items:
            LOG.warning("no series to publish")
            return
        if DRY_RUN:
            for s in self.items:
                print(json.dumps(s), flush=True)
            return
        from mail_infra.metrics_sink import publish, sink_is_redis

        if not sink_is_redis():
            LOG.error("CONNECT_METRICS_SINK is not 'redis'; %d series not published", len(self.items))
            return
        # Prometheus through Redis: the API serves the series on GET /metrics
        publish("delivery_stats", self.items, ttl_sec=WINDOW_SEC * 3)


def _bucket(cnx) -> tuple[int, int]:
    """Last CLOSED bucket of WINDOW_SEC, by the MySQL clock.

    Aligning to the database clock (not the host's) keeps the buckets contiguous even when
    a run starts late: otherwise two delayed runs could count the same event twice or leave
    a gap in the series.
    """
    cur = cnx.cursor(dictionary=True)
    cur.execute(
        "SELECT FLOOR(UNIX_TIMESTAMP(NOW())/%s)*%s AS bucket_end", (WINDOW_SEC, WINDOW_SEC)
    )
    end = int((cur.fetchone() or {}).get("bucket_end") or 0)
    return end - WINDOW_SEC, end


def _relay_by_sending_domain(cnx) -> tuple[dict[str, str], str]:
    """sending_domain -> effective relay (explicit binding or the database default)."""
    cur = cnx.cursor(dictionary=True)
    cur.execute("SELECT name FROM relays WHERE is_default = 1 LIMIT 1")
    row = cur.fetchone()
    default_relay = (row or {}).get("name") or "none"

    cur.execute(
        """
        SELECT vd.name AS sending_domain, r.name AS relay_name
        FROM email_configs ec
        JOIN virtual_domains vd ON vd.id = ec.domain_id
        LEFT JOIN zone_relays zr ON zr.zone_domain = vd.name
        LEFT JOIN relays r ON r.id = zr.relay_id
        WHERE ec.is_active = 1
        """
    )
    out: dict[str, str] = {}
    for r in cur.fetchall() or []:
        out[r["sending_domain"]] = r["relay_name"] or f"default-{default_relay}"
    return out, default_relay


def _delivery_counts(cnx, s: Series, start: int, end: int) -> None:
    """Events of the bucket by status x egress x sender.

    The egress is the delivery_events host: the hostname of the node that delivered the
    message (the control node or a sending node).
    """
    cur = cnx.cursor(dictionary=True)
    egress_expr = "COALESCE(NULLIF(de.host, ''), 'unknown')"
    cur.execute(
        f"""
        SELECT de.status,
               {egress_expr} AS egress,
               CASE
                 WHEN vd.id IS NULL THEN '_unattributed'
                 WHEN de.domain_name LIKE 'rbm%%'
                   THEN SUBSTRING(de.domain_name, LOCATE('.', de.domain_name)+1)
                 ELSE de.domain_name
               END AS sender_domain,
               COUNT(*) AS n
        FROM delivery_events de
        LEFT JOIN virtual_domains vd ON vd.name = de.domain_name {COLL}
        WHERE de.ts >= FROM_UNIXTIME(%s) AND de.ts < FROM_UNIXTIME(%s)
        GROUP BY de.status, egress, sender_domain
        """,
        (start, end),
    )
    for r in cur.fetchall() or []:
        s.count(
            "mail.delivery.events",
            int(r["n"]),
            status=r["status"],
            egress=r["egress"],
            sender_domain=r["sender_domain"],
        )


def _dsn_counts(cnx, s: Series, start: int, end: int) -> None:
    """DSN codes of the failed events in the bucket: the "why" of a bounce or deferral.

    Only the status code itself (LEFT(dsn, 5), e.g. 5.1.1): the full DSN carries free text
    from the remote MX and would blow up the cardinality.
    """
    cur = cnx.cursor(dictionary=True)
    cur.execute(
        """
        SELECT status, LEFT(dsn, 5) AS code, COUNT(*) AS n
        FROM delivery_events
        WHERE ts >= FROM_UNIXTIME(%s) AND ts < FROM_UNIXTIME(%s)
          AND status IN ('bounced', 'deferred')
          AND dsn IS NOT NULL AND dsn <> ''
        GROUP BY status, code
        ORDER BY n DESC
        LIMIT 40
        """,
        (start, end),
    )
    for r in cur.fetchall() or []:
        s.count("mail.delivery.dsn", int(r["n"]), dsn=(r["code"] or "").strip(), status=r["status"])


def _attribution(cnx, s: Series) -> None:
    """% of the last 24 h of events with an identifiable sender (see the module docstring)."""
    cur = cnx.cursor(dictionary=True)
    cur.execute(
        f"""
        SELECT COUNT(*) AS total,
               SUM(CASE WHEN vd.id IS NOT NULL THEN 1 ELSE 0 END) AS attributed
        FROM delivery_events de
        LEFT JOIN virtual_domains vd ON vd.name = de.domain_name {COLL}
        WHERE de.ts >= NOW() - INTERVAL 1 DAY
        """
    )
    r = cur.fetchone() or {}
    total = int(r.get("total") or 0)
    attributed = int(r.get("attributed") or 0)
    s.gauge("mail.delivery.attributed_ratio", _pct(attributed, total))
    s.gauge("mail.delivery.events_24h", total)


def _sender_reputation(cnx, s: Series, relay_map: dict[str, str]) -> None:
    """Reputation per SENDER domain over 24 h, attributed rows only."""
    cur = cnx.cursor(dictionary=True)
    cur.execute(
        f"""
        SELECT CASE WHEN de.domain_name LIKE 'rbm%%'
                      THEN SUBSTRING(de.domain_name, LOCATE('.', de.domain_name)+1)
                    ELSE de.domain_name END AS sender_domain,
               COUNT(*) AS attempts,
               SUM(de.status = 'delivered') AS delivered,
               SUM(de.status = 'deferred')  AS deferred,
               SUM(de.status = 'bounced' OR de.dsn LIKE '5.%%') AS hard_bounced
        FROM delivery_events de
        JOIN virtual_domains vd ON vd.name = de.domain_name {COLL}
        WHERE de.ts >= NOW() - INTERVAL 1 DAY
        GROUP BY sender_domain
        HAVING attempts >= %s
        """,
        (SENDER_MIN_VOL,),
    )
    for r in cur.fetchall() or []:
        domain = r["sender_domain"]
        attempts = int(r["attempts"])
        delivered = int(r["delivered"] or 0)
        relay = relay_map.get(domain, "unknown")
        s.gauge("mail.sender.attempts_24h", attempts, sender_domain=domain, relay=relay)
        s.gauge("mail.sender.delivery_rate", _pct(delivered, attempts),
                sender_domain=domain, relay=relay)
        s.gauge("mail.sender.hard_bounce_rate", _pct(int(r["hard_bounced"] or 0), attempts),
                sender_domain=domain, relay=relay)
        s.gauge("mail.sender.defer_rate", _pct(int(r["deferred"] or 0), attempts),
                sender_domain=domain, relay=relay)


def _ledger(cnx, s: Series, start: int, end: int) -> None:
    """send_ledger: current backlog, terminal transitions of the bucket and stalled rows."""
    cur = cnx.cursor(dictionary=True)

    cur.execute(
        """
        SELECT state, COUNT(*) AS n FROM send_ledger
        WHERE state IN ('queued','sending','smtp_accepted','deferred')
        GROUP BY state
        """
    )
    backlog = {r["state"]: int(r["n"]) for r in (cur.fetchall() or [])}
    for state in ("queued", "sending", "smtp_accepted", "deferred"):
        s.gauge("mail.ledger.backlog", backlog.get(state, 0), state=state)

    cur.execute(
        """
        SELECT state, COALESCE(customer_id, 'unknown') AS customer_id, COUNT(*) AS n
        FROM send_ledger
        WHERE terminal_at >= FROM_UNIXTIME(%s) AND terminal_at < FROM_UNIXTIME(%s)
        GROUP BY state, customer_id
        """,
        (start, end),
    )
    for r in cur.fetchall() or []:
        s.count("mail.ledger.terminal", int(r["n"]),
                state=r["state"], customer_id=r["customer_id"])

    cur.execute(
        """
        SELECT last_smtp_code AS code, COUNT(*) AS n
        FROM send_ledger
        WHERE updated_at >= FROM_UNIXTIME(%s) AND updated_at < FROM_UNIXTIME(%s)
          AND last_smtp_code IS NOT NULL
        GROUP BY last_smtp_code
        ORDER BY n DESC
        LIMIT 30
        """,
        (start, end),
    )
    for r in cur.fetchall() or []:
        s.count("mail.smtp.code", int(r["n"]), code=str(r["code"]))

    # Same predicates as group_health: while the reaper is running this should be zero.
    cur.execute(
        """
        SELECT
          SUM(state='smtp_accepted' AND delivery_deadline IS NOT NULL
                AND delivery_deadline < NOW() - INTERVAL %s SECOND) AS accepted,
          SUM(state='queued' AND admitted_at < NOW() - INTERVAL %s SECOND) AS queued,
          SUM(state='deferred' AND next_attempt_at IS NOT NULL
                AND next_attempt_at < NOW() - INTERVAL %s SECOND) AS deferred,
          SUM(state='sending' AND lease_expires_at IS NOT NULL
                AND lease_expires_at < NOW() - INTERVAL %s SECOND) AS sending
        FROM send_ledger
        WHERE state IN ('smtp_accepted','queued','deferred','sending')
        """,
        (STUCK_GRACE_SEC,) * 4,
    )
    st = cur.fetchone() or {}
    for kind in ("accepted", "queued", "deferred", "sending"):
        s.gauge("mail.ledger.stuck", int(st.get(kind) or 0), kind=kind)


def _outbox(cnx, s: Series) -> None:
    cur = cnx.cursor(dictionary=True)
    cur.execute(
        "SELECT COUNT(*) AS pending, "
        "COALESCE(TIMESTAMPDIFF(SECOND, MIN(created_at), NOW()), 0) AS oldest_sec "
        "FROM kafka_outbox WHERE status = 'pending'"
    )
    r = cur.fetchone() or {}
    s.gauge("mail.outbox.pending", int(r.get("pending") or 0))
    s.gauge("mail.outbox.oldest_pending_sec", int(r.get("oldest_sec") or 0))


def _groups(cnx, s: Series) -> None:
    """Occupancy of the shared node groups.

    Note: `load_pct` is RELATIVE TO A CONFIGURED CONSTANT, not to a measured ceiling:
    DS_GROUP_DAILY_CAP is the same value for every shared group, whatever the number of
    nodes/IPs in it. It is useful to compare groups and to spot relative saturation; it does
    NOT mean that delivery fails above 100%. That is why `mail.group.daily_cap` is emitted
    alongside it: it keeps the assumption visible.
    """
    cur = cnx.cursor(dictionary=True)
    cur.execute(
        f"""
        SELECT vg.group_key, vg.provider,
               COALESCE(ld.delivered, 0) AS delivered,
               (SELECT COUNT(*) FROM zones z2
                  WHERE z2.group_id = vg.id
                    AND z2.status = 'success_provision_infrastructure'
                    AND NOT EXISTS (SELECT 1 FROM customer_zones cz WHERE cz.zone_id = z2.id)
               ) AS free_zones,
               (SELECT COUNT(DISTINCT cz2.customer_id) FROM customer_zones cz2
                  JOIN zones z3 ON z3.id = cz2.zone_id
                  WHERE z3.group_id = vg.id) AS tenants
        FROM vm_groups vg
        LEFT JOIN (
            SELECT z.group_id, SUM(de.status = 'delivered') AS delivered
            FROM delivery_events de
            JOIN zones z ON z.subdomain_customer = SUBSTRING_INDEX(de.domain_name, '.', 1) {COLL}
            WHERE de.ts >= NOW() - INTERVAL 1 DAY
            GROUP BY z.group_id
        ) ld ON ld.group_id = vg.id
        WHERE vg.kind = 'shared' AND vg.status = 'active'
        """
    )
    for r in cur.fetchall() or []:
        key = r["group_key"]
        provider = r["provider"]
        delivered = int(r["delivered"] or 0)
        s.gauge("mail.group.delivered_per_day", delivered, group_key=key, provider=provider)
        s.gauge("mail.group.load_pct", _pct(delivered, GROUP_DAILY_CAP),
                group_key=key, provider=provider)
        s.gauge("mail.group.daily_cap", GROUP_DAILY_CAP, group_key=key, provider=provider)
        s.gauge("mail.group.free_zones", int(r["free_zones"] or 0),
                group_key=key, provider=provider)
        # Co-tenants: a shared group mixes customers that share the same IPs, and the
        # group's label rarely tells how many. Without this gauge the blast radius of a
        # reputation problem in the group is invisible.
        s.gauge("mail.group.tenants", int(r["tenants"] or 0),
                group_key=key, provider=provider)


def main() -> int:
    from connect_lib.logging import setup_logging

    setup_logging(service_name="mail-delivery-stats", json_format=True)

    from mail_infra.metrics_sink import sink_is_redis

    if not DRY_RUN and not sink_is_redis():
        LOG.error("CONNECT_METRICS_SINK is not 'redis' and there is no other metrics sink; aborting")
        return 2

    from mail_infra.infrastructure.database import get_connection

    with get_connection() as cnx:
        start, end = _bucket(cnx)
        s = Series(start)
        relay_map, default_relay = _relay_by_sending_domain(cnx)

        # Every block is independent: a failing SELECT (a schema difference, a missing
        # table) must not stop the rest of the collection.
        for name, fn in (
            ("delivery_counts", lambda: _delivery_counts(cnx, s, start, end)),
            ("dsn_counts", lambda: _dsn_counts(cnx, s, start, end)),
            ("attribution", lambda: _attribution(cnx, s)),
            ("sender_reputation", lambda: _sender_reputation(cnx, s, relay_map)),
            ("ledger", lambda: _ledger(cnx, s, start, end)),
            ("outbox", lambda: _outbox(cnx, s)),
            ("groups", lambda: _groups(cnx, s)),
        ):
            try:
                fn()
            except Exception:
                LOG.exception("block %s failed; continuing with the rest of the collection", name)

    s.submit()
    print(json.dumps({
        "event": "delivery_stats_done",
        "series": len(s.items),
        "bucket_start": start,
        "window_sec": WINDOW_SEC,
        "default_relay": default_relay,
        "env": ENV,
        "dry_run": DRY_RUN,
    }), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
