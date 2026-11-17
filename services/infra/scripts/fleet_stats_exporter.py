#!/usr/bin/env python3
"""Fleet stats exporter — what only MySQL knows about the sending nodes: how many domains
each node serves and the actual deliveries per node.

The source is the database (zone_relays + delivery_events). The series are published through
mail_infra.metrics_sink (CONNECT_METRICS_SINK=redis) and the API serves them on GET /metrics
(Prometheus).

Emits (gauge, tags group:<node|none>, pool:<relay name>):
  mail_fleet.domains_assigned   # active domains routed to the node
  mail_fleet.delivered_1h       # actual deliveries (sent+delivered) in the window
  mail_fleet.deferred_1h
  mail_fleet.bounced_1h
  mail_fleet.delivery_rate      # % deliverability = delivered/(delivered+bounced)
And global ones (no pool): mail_fleet.domains_total, mail_fleet.pools_active.

FS_WINDOW_HOURS (default 1). FS_DRY_RUN=true prints the series instead of publishing them.
"""

from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

WINDOW_HOURS = int(os.getenv("FS_WINDOW_HOURS", "1"))
DRY_RUN = os.getenv("FS_DRY_RUN", "false").lower() in ("1", "true", "yes", "on")
ENV = os.getenv("ENVIRONMENT", "prod")

# relay name -> group tag. In RevBridge Connect every relay is one of the installation's own
# nodes; "none" means there is no relay (no binding and no default relay).
def group_of(relay_name: str | None) -> str:
    if not relay_name or relay_name == "none":
        return "none"
    return "node"


def now_ts() -> int:
    import time
    return int(time.time())


def submit(series: list[dict]) -> None:
    if DRY_RUN or not series:
        for s in series:
            print(json.dumps(s))
        return
    from mail_infra.metrics_sink import publish, sink_is_redis

    if not sink_is_redis():
        raise RuntimeError("CONNECT_METRICS_SINK is not 'redis'; there is no other metrics sink")
    # Prometheus through Redis: the API serves the series on GET /metrics
    publish("fleet_stats", series, ttl_sec=WINDOW_HOURS * 3600 * 3)


def main() -> int:
    from mail_infra.infrastructure.database import get_connection

    ts = now_ts()
    series: list[dict] = []

    def gauge(metric: str, value: float, group: str, pool: str | None = None) -> None:
        tags = [f"group:{group}", "service:mail-fleet", f"env:{ENV}"]
        if pool:
            tags.append(f"pool:{pool}")
        series.append({"metric": metric, "points": [[ts, value]], "type": "gauge", "tags": tags})

    with get_connection() as cnx:
        cur = cnx.cursor(dictionary=True)

        # --- inventory: each active domain -> node (longest suffix match, or the default relay) ---
        cur.execute("SELECT id, name FROM relays WHERE is_default=1 LIMIT 1")
        drow = cur.fetchone()
        default_relay = drow["name"] if drow else "none"

        cur.execute(
            """
            SELECT vd.name AS domain,
              (SELECT r.name FROM zone_relays zr JOIN relays r ON r.id=zr.relay_id
                 WHERE vd.name LIKE CONCAT('%', zr.zone_domain)
                 ORDER BY LENGTH(zr.zone_domain) DESC LIMIT 1) AS relay
            FROM virtual_domains vd
            JOIN email_configs ec ON ec.domain_id = vd.id AND ec.is_active = 1
            WHERE vd.name NOT LIKE 'rbm%'
            GROUP BY vd.name
            """
        )
        per_pool_domains: dict[tuple[str, str], int] = {}
        total_domains = 0
        for row in cur.fetchall():
            total_domains += 1
            relay = row["relay"] or default_relay
            key = (group_of(relay), relay)
            per_pool_domains[key] = per_pool_domains.get(key, 0) + 1
        for (group, pool), n in per_pool_domains.items():
            gauge("mail_fleet.domains_assigned", n, group, pool)
        gauge("mail_fleet.domains_total", total_domains, "all")
        gauge("mail_fleet.pools_active", len(per_pool_domains), "all")

        # --- actual deliveries per node: the delivery_events host is the node's hostname
        # (= relays.name) ---
        cur.execute(
            """
            SELECT de.host AS pool,
                   SUM(de.status IN ('sent','delivered')) AS delivered,
                   SUM(de.status='deferred') AS deferred,
                   SUM(de.status='bounced')  AS bounced
            FROM delivery_events de JOIN relays r ON r.name = de.host
            WHERE de.created_at >= NOW() - INTERVAL %s HOUR
            GROUP BY de.host
            """,
            (WINDOW_HOURS,),
        )
        for row in cur.fetchall():
            pool = row["pool"]
            if not pool:
                continue
            deliv = int(row["delivered"] or 0)
            deferred = int(row["deferred"] or 0)
            bounced = int(row["bounced"] or 0)
            gauge("mail_fleet.delivered_1h", deliv, "node", pool)
            gauge("mail_fleet.deferred_1h", deferred, "node", pool)
            gauge("mail_fleet.bounced_1h", bounced, "node", pool)
            denom = deliv + bounced
            if denom > 0:
                gauge("mail_fleet.delivery_rate", round(100.0 * deliv / denom, 1), "node", pool)

    submit(series)
    print(json.dumps({"event": "fleet_stats_done", "metrics": len(series),
                      "domains_total": total_domains, "dry_run": DRY_RUN}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
