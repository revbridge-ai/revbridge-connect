#!/usr/bin/env python3
"""Egress tier controller — automatic upgrade/downgrade between shared and dedicated nodes.

The plan is declared in `egress_tier_plan` (zone -> designated dedicated relay, see
config/db/initdb/032_infra_egress_tier.sql). On every run (hourly):

  1. Measures the zone's volume in `delivery_events` (sends at the control node's hop:
     host LIKE CONTROL_HOST_LIKE, domain_name = envelope rbm<id>.<zone>).
  2. Decides:  upgrade   if NOT dedicated and the 7-day average  >= up_daily
               downgrade if dedicated     and the 14-day average <  down_daily
  3. Gates: cooldown (min_days_between_moves), mode=auto, and on upgrade a TCP :25 probe of
     the dedicated relay's host (a node that is down is never chosen).
  4. Acts on `zone_relays` (bind = upgrade; removing the bind = downgrade: the zone goes back
     to the shared node through a suffix bind or the default relay). It takes effect within
     the trivial-rewrite cache TTL (minutes), with no reload or redeploy.

TC_DRY_RUN=true (default) -> decides and logs, does NOT act. Audit trail in
`egress_tier_log` + JSON events on stdout.
"""

from __future__ import annotations

import json
import os
import socket
import sys
from dataclasses import dataclass

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

DRY_RUN = os.getenv("TC_DRY_RUN", "true").lower() in ("1", "true", "yes", "on")
UP_WINDOW_DAYS = int(os.getenv("TC_UP_WINDOW_DAYS", "7"))
DOWN_WINDOW_DAYS = int(os.getenv("TC_DOWN_WINDOW_DAYS", "14"))
PROBE_TIMEOUT = int(os.getenv("TC_PROBE_TIMEOUT", "10"))
# Automatic quarantine of a toxic node (a high 5.7.x rate = the IP is rejected because of its
# range, reputation or a blocklist). Fail-safe: its zones fall back to the shared node or the
# default relay.
Q_ENABLED = os.getenv("TC_QUARANTINE_ENABLED", "false").lower() in ("1", "true", "yes", "on")
Q_WINDOW_MIN = int(os.getenv("TC_Q_WINDOW_MIN", "30"))
Q_MIN_EVENTS = int(os.getenv("TC_Q_MIN_EVENTS", "100"))
Q_MAX_RATIO = float(os.getenv("TC_Q_MAX_RATIO", "0.3"))


def emit(event: str, **kw) -> None:
    print(json.dumps({"event": event, **kw}, default=str), flush=True)


@dataclass
class PlanRow:
    zone_domain: str
    dedicated_relay_id: int
    mode: str
    up_daily: int
    down_daily: int
    min_days_between_moves: int
    days_since_move: float | None  # None = never moved


def decide(*, bound_dedicated: bool, avg_up: float, avg_down: float,
           up_daily: int, down_daily: int) -> str | None:
    """Pure tier decision (testable): upgrade | downgrade | None."""
    if not bound_dedicated and avg_up >= up_daily:
        return "upgrade"
    if bound_dedicated and avg_down < down_daily:
        return "downgrade"
    return None


def probe_smtp(host: str, port: int = 25, timeout: int = PROBE_TIMEOUT) -> bool:
    """SMTP 220 banner = the node is alive."""
    try:
        with socket.create_connection((host, port), timeout=timeout) as s:
            s.settimeout(timeout)
            banner = s.recv(128)
            s.sendall(b"QUIT\r\n")
            return banner.startswith(b"220")
    except OSError:
        return False


def daily_avg(cur, zone: str, days: int) -> float:
    """Daily average of the zone's sends at the control node's hop (delivery_events)."""
    from mail_infra.topology import CONTROL_HOST_LIKE

    cur.execute(
        """
        SELECT COUNT(*) FROM delivery_events
        WHERE host LIKE %s
          AND status IN ('sent', 'delivered')
          AND (domain_name = %s OR domain_name LIKE CONCAT('%%.', %s))
          AND created_at >= NOW() - INTERVAL %s DAY
        """,
        (CONTROL_HOST_LIKE, zone, zone, days),
    )
    (total,) = cur.fetchone()
    return (total or 0) / max(days, 1)


def log_action(cur, zone: str, action: str, detail: str) -> None:
    cur.execute(
        "INSERT INTO egress_tier_log (zone_domain, action, detail) VALUES (%s, %s, %s)",
        (zone, action, detail[:2000]),
    )


def should_quarantine(*, rejected: int, delivered: int,
                      min_events: int, max_ratio: float) -> bool:
    """Pure node-quarantine decision (testable).

    rejected = POLICY bounces (dsn 5.7.x) in the window: the IP's range, reputation or a
    blocklist (e.g. "not authorized", "blocked using Spamhaus"). List hard bounces (5.1.1
    etc.) do NOT count: they are the sender's problem, not the IP's. Quarantine needs a
    minimum volume AND a high ratio (so a handful of rejections on a nearly idle node does
    not trigger it).
    """
    total = rejected + delivered
    if rejected < min_events or total == 0:
        return False
    return (rejected / total) >= max_ratio


def pool_quarantine_sweep(cur) -> int:
    """Checks the health of every sending node and QUARANTINES the toxic ones.

    Signal: delivery_events of the node's host (window TC_Q_WINDOW_MIN).
    Action: removes ALL of the relay's binds (its zones fall back to the shared node by
    suffix or to the default relay) + egress_tier_plan.mode='manual' (the controller does not
    move them back on its own; re-binding is a human decision once the IP is fixed). A range
    rejection is then contained within one cycle instead of hours.
    """
    from mail_infra.topology import NODE_RELAY_LIKE, node_host_like

    quarantined = 0
    cur.execute("SELECT id, name FROM relays WHERE name LIKE %s AND smtp_host <> 'direct'", (NODE_RELAY_LIKE,))
    for relay_id, relay_name in cur.fetchall():
        cur.execute(
            """
            SELECT
              SUM(status='bounced' AND dsn LIKE '5.7.%%'),
              SUM(status IN ('sent','delivered'))
            FROM delivery_events
            WHERE host LIKE %s
              AND created_at >= NOW() - INTERVAL %s MINUTE
            """,
            (node_host_like(relay_name), Q_WINDOW_MIN),
        )
        rejected, delivered = (int(x or 0) for x in cur.fetchone())
        emit("pool_health", relay=relay_name, rejected_57x=rejected, delivered=delivered)
        if not should_quarantine(rejected=rejected, delivered=delivered,
                                 min_events=Q_MIN_EVENTS, max_ratio=Q_MAX_RATIO):
            continue

        cur.execute("SELECT zone_domain FROM zone_relays WHERE relay_id=%s", (relay_id,))
        zones = [r[0] for r in cur.fetchall()]
        detail = json.dumps({"relay": relay_name, "rejected_57x": rejected,
                             "delivered": delivered, "zones": zones})
        if DRY_RUN or not Q_ENABLED:
            log_action(cur, relay_name, "dry_run:quarantine", detail)
            emit("pool_quarantine", relay=relay_name, dry_run=True, zones=zones,
                 rejected_57x=rejected, delivered=delivered)
            continue

        cur.execute("DELETE FROM zone_relays WHERE relay_id=%s", (relay_id,))
        cur.execute(
            "UPDATE egress_tier_plan SET mode='manual', last_decision='quarantined'"
            " WHERE dedicated_relay_id=%s",
            (relay_id,),
        )
        log_action(cur, relay_name, "quarantine", detail)
        emit("pool_quarantine", relay=relay_name, dry_run=False, zones=zones,
             rejected_57x=rejected, delivered=delivered)
        quarantined += 1
    return quarantined


def main() -> int:
    from mail_infra.infrastructure.database import get_connection

    moved = 0
    with get_connection() as cnx:
        cur = cnx.cursor()
        cur.execute(
            """
            SELECT p.zone_domain, p.dedicated_relay_id, p.mode, p.up_daily,
                   p.down_daily, p.min_days_between_moves,
                   TIMESTAMPDIFF(HOUR, p.last_moved_at, NOW()) / 24.0
            FROM egress_tier_plan p
            """
        )
        plans = [PlanRow(*row) for row in cur.fetchall()]

        # Node health BEFORE the tier decisions (runs even when there are no plans).
        quarantined = pool_quarantine_sweep(cur)

        if not plans:
            cnx.commit()
            emit("done", moved=0, quarantined=quarantined, dry_run=DRY_RUN, plans=0)
            return 0

        for plan in plans:
            zone = plan.zone_domain

            cur.execute(
                "SELECT r.id, r.name, r.smtp_host, r.smtp_port FROM relays r WHERE r.id = %s",
                (plan.dedicated_relay_id,),
            )
            relay = cur.fetchone()
            if not relay:
                emit("relay_missing", zone=zone, relay_id=plan.dedicated_relay_id)
                log_action(cur, zone, "skip:relay_missing", f"relay_id={plan.dedicated_relay_id}")
                continue
            relay_id, relay_name, relay_host, relay_port = relay

            cur.execute(
                "SELECT relay_id FROM zone_relays WHERE zone_domain = %s", (zone,)
            )
            bind = cur.fetchone()
            bound_dedicated = bool(bind) and int(bind[0]) == int(relay_id)

            avg_up = daily_avg(cur, zone, UP_WINDOW_DAYS)
            avg_down = daily_avg(cur, zone, DOWN_WINDOW_DAYS)
            action = decide(
                bound_dedicated=bound_dedicated,
                avg_up=avg_up,
                avg_down=avg_down,
                up_daily=plan.up_daily,
                down_daily=plan.down_daily,
            )
            obs = {
                "zone": zone, "relay": relay_name, "bound_dedicated": bound_dedicated,
                "avg7": round(avg_up, 1), "avg14": round(avg_down, 1),
                "up_daily": plan.up_daily, "down_daily": plan.down_daily,
            }
            emit("tier_obs", **obs)

            if action is None:
                continue
            if plan.mode != "auto":
                log_action(cur, zone, "skip:manual_mode", json.dumps(obs))
                emit("tier_skip", reason="manual_mode", **obs)
                continue
            if plan.days_since_move is not None and plan.days_since_move < plan.min_days_between_moves:
                log_action(cur, zone, "skip:cooldown", json.dumps(obs))
                emit("tier_skip", reason="cooldown", days_since_move=plan.days_since_move, **obs)
                continue
            if action == "upgrade" and not probe_smtp(relay_host, int(relay_port)):
                log_action(cur, zone, "skip:probe_failed", f"{relay_host}:{relay_port}")
                emit("tier_skip", reason="probe_failed", host=relay_host, **obs)
                continue

            if DRY_RUN:
                log_action(cur, zone, f"dry_run:{action}", json.dumps(obs))
                cur.execute(
                    "UPDATE egress_tier_plan SET last_decision=%s WHERE zone_domain=%s",
                    (f"dry_run:{action}", zone),
                )
                emit("tier_decision", action=action, dry_run=True, **obs)
                continue

            if action == "upgrade":
                cur.execute(
                    "INSERT INTO zone_relays (zone_domain, relay_id) VALUES (%s, %s)"
                    " ON DUPLICATE KEY UPDATE relay_id = VALUES(relay_id)",
                    (zone, relay_id),
                )
            else:  # downgrade: remove the bind -> shared node (suffix) or default relay
                cur.execute("DELETE FROM zone_relays WHERE zone_domain = %s", (zone,))

            cur.execute(
                "UPDATE egress_tier_plan SET last_moved_at=NOW(), last_decision=%s"
                " WHERE zone_domain=%s",
                (action, zone),
            )
            log_action(cur, zone, action, json.dumps(obs))
            emit("tier_decision", action=action, dry_run=False, **obs)
            moved += 1

        cnx.commit()

    emit("done", moved=moved, quarantined=quarantined, dry_run=DRY_RUN, plans=len(plans))
    return 0


if __name__ == "__main__":
    sys.exit(main())
