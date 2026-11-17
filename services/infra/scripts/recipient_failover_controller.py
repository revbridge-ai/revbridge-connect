#!/usr/bin/env python3
"""Egress failover by RECIPIENT DOMAIN, based on DELIVERY results (recipient_relays).

The other controllers decide by sender volume (zones) or by IP health (DNSBL/FCrDNS); none of
them looks at the delivery RESULT. This one does exactly that: per (recipient domain x node),
it measures deferrals/bounces/blocks over the window. When a receiver HOLDS/REJECTS a node's
IPs (5.7.1 cold reputation, 4.7.1 greylisting, a high deferral rate) EVEN with a DNSBL-clean
IP, it fails that recipient domain over to another relay.

It acts on the `recipient_relays` table (Postfix reads it through transport_maps, which TAKES
PRECEDENCE over sender_dependent_relayhost_maps). Since its scope is the RECIPIENT, while the
sender-side controllers act on `zone_relays` (SENDER), the two never conflict.

Recovery (auto-heal): AUTO rows (notes starting with 'auto-failover:') that have been failed
over for more than COOLDOWN are reopened (is_active=0) to probe the original route again. If
it fails again in the next window, the row is re-activated; if it delivers cleanly, the row
stays dormant. MANUAL rows (without the 'auto-failover:' prefix) are NEVER touched.

RF_DRY_RUN=true (default) -> decides and logs, does NOT act.

RevBridge Connect (no provider, CONNECT_OVERFLOW_RELAY empty): the failover target is the
healthy OWN node with the most spare capacity (mail_infra.topology.pick_target_node), measured
per node and per recipient domain. With no node available there is no failover: the message
stays in the queue and Postfix retries. When an operator configures an overflow relay
(CONNECT_OVERFLOW_RELAY), failing recipient domains go to that relay instead; the decision
(decide) and the auto-heal are the same in both modes.
"""

from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

DRY_RUN = os.getenv("RF_DRY_RUN", "true").lower() in ("1", "true", "yes", "on")
WINDOW_MIN = int(os.getenv("RF_WINDOW_MIN", "60"))
# minimum volume of the recipient domain in the window to have a statistical signal (avoids noise).
MIN_EVENTS = int(os.getenv("RF_MIN_EVENTS", "20"))
# Fail over ONLY when another IP would help: a rejection because of the REPUTATION/POLICY of
# OUR IP. Signal = 5.7.x/4.7.x blocks (rejecting our IP) and sustained deferrals
# (greylisting/rate limits). Generic bounces (5.1.1 unknown address, 5.4.x broken MX) do NOT
# trigger it: they would fail through any relay, so changing the relay does not help.
# % of block events (5.7.x/4.7.x) over the total that triggers a failover.
BLOCK_PCT = float(os.getenv("RF_BLOCK_PCT", "8"))
# % of sustained deferrals over the total that triggers it (the receiver is holding our IP).
DEFER_PCT = float(os.getenv("RF_DEFER_PCT", "40"))
# hours on the failover route (auto row) before reopening it to probe the original route.
COOLDOWN_H = int(os.getenv("RF_COOLDOWN_H", "6"))
AUTO_TAG = "auto-failover:"

# Overflow-relay mode only: delivery_events host pattern (final hop) of the events that are
# evaluated. Domains already on the overflow relay that keep failing are skipped below.
POOL_HOST_LIKE = os.getenv("RF_POOL_HOST_LIKE", "%")


def emit(event: str, **kw) -> None:
    print(json.dumps({"event": event, **kw}, default=str), flush=True)


def decide(*, deferred: int, bounced: int, sent: int, block: int) -> dict:
    """Pure decision (testable). failing=True -> fail over to another relay.

    Fires on REPUTATION/POLICY (5.7.x/4.7.x blocks) or on sustained deferral, the cases where
    moving to another IP helps. Does NOT fire on generic bounces (bad address/MX), which no
    other relay would deliver either.
    """
    total = sent + deferred + bounced
    out = {"total": total, "block_pct": 0.0, "defer_pct": 0.0}
    if total < MIN_EVENTS:
        return {**out, "failing": False, "reason": "insufficient"}
    block_pct = round(100.0 * block / total, 2)
    defer_pct = round(100.0 * deferred / total, 2)
    out.update(block_pct=block_pct, defer_pct=defer_pct)
    if block_pct >= BLOCK_PCT:
        return {**out, "failing": True, "reason": "block"}
    if defer_pct >= DEFER_PCT:
        return {**out, "failing": True, "reason": "defer"}
    return {**out, "failing": False, "reason": "healthy"}


def _upsert_failover(cur, rdom: str, relay_id: int, note: str) -> None:
    cur.execute(
        """
        INSERT INTO recipient_relays (recipient_domain, relay_id, is_active, notes)
        VALUES (%s, %s, 1, %s)
        ON DUPLICATE KEY UPDATE relay_id=VALUES(relay_id),
            is_active=1, notes=VALUES(notes)
        """,
        (rdom, relay_id, note),
    )


def main_own_nodes(cur) -> tuple[int, int, int]:
    """RevBridge Connect: failover by recipient domain between OWN nodes (no provider)."""
    from mail_infra.topology import list_nodes, node_host_like, pick_target_node

    moved = 0
    candidates = 0
    nodes = list_nodes(cur)
    failing_by_rdom: dict[str, list[tuple[dict, dict]]] = {}
    for n in nodes:
        cur.execute(
            """
            SELECT LOWER(SUBSTRING_INDEX(rcpt, '@', -1)) AS rdom,
                   SUM(status IN ('sent','delivered'))                  AS sent,
                   SUM(status='deferred')                               AS deferred,
                   SUM(status='bounced')                                AS bounced,
                   SUM(dsn LIKE '5.7.%%' OR dsn LIKE '4.7.%%')          AS block
            FROM delivery_events
            WHERE host LIKE %s
              AND ts >= NOW() - INTERVAL %s MINUTE
              AND rcpt LIKE '%%@%%'
            GROUP BY rdom
            HAVING COUNT(*) >= %s AND (SUM(status='deferred') + SUM(status='bounced')) > 0
            """,
            (node_host_like(n["name"]), WINDOW_MIN, MIN_EVENTS),
        )
        for r in cur.fetchall():
            candidates += 1
            d = decide(deferred=int(r["deferred"] or 0), bounced=int(r["bounced"] or 0),
                       sent=int(r["sent"] or 0), block=int(r["block"] or 0))
            if d["failing"]:
                failing_by_rdom.setdefault(r["rdom"], []).append((n, d))

    for rdom, failing in failing_by_rdom.items():
        cur.execute(
            "SELECT relay_id, is_active, notes FROM recipient_relays WHERE recipient_domain=%s", (rdom,))
        ex = cur.fetchone()
        if ex and int(ex["is_active"]) == 1 and not (ex["notes"] or "").startswith(AUTO_TAG):
            emit("skip_manual", rdom=rdom)
            continue
        bad_ids = {int(n["id"]) for n, _ in failing}
        target = pick_target_node(cur, exclude_ids=bad_ids)
        worst_node, d = max(failing, key=lambda nd: (nd[1]["block_pct"], nd[1]["defer_pct"]))
        if not target:
            emit("no_target", rdom=rdom, failing_on=[n["name"] for n, _ in failing], **d)
            continue
        if ex and int(ex["is_active"]) == 1 and int(ex["relay_id"]) == int(target["id"]):
            continue
        note = (f"{AUTO_TAG} {d['reason']} blk={d['block_pct']}% def={d['defer_pct']}% "
                f"n={d['total']} w={WINDOW_MIN}m from={worst_node['name']} to={target['name']}")
        emit("failover", rdom=rdom, frm=worst_node["name"], to=target["name"], dry_run=DRY_RUN, **d)
        if not DRY_RUN:
            _upsert_failover(cur, rdom, int(target["id"]), note[:255])
            moved += 1
    return moved, candidates, len(failing_by_rdom)


def main() -> int:
    from mail_infra.infrastructure.database import get_connection
    from mail_infra.topology import overflow_relay_id

    moved = 0
    reopened = 0
    with get_connection() as cnx:
        cur = cnx.cursor(dictionary=True)

        overflow_id = overflow_relay_id(cur)
        if not overflow_id:
            # RevBridge Connect: no provider -> failover between own nodes + auto-heal.
            moved, candidates, failing = main_own_nodes(cur)
            cur.execute(
                """
                SELECT recipient_domain FROM recipient_relays
                WHERE is_active=1 AND notes LIKE %s
                  AND updated_at <= NOW() - INTERVAL %s HOUR
                """,
                (AUTO_TAG + "%", COOLDOWN_H),
            )
            for x in cur.fetchall():
                emit("reopen_probe", rdom=x["recipient_domain"], dry_run=DRY_RUN)
                if not DRY_RUN:
                    cur.execute(
                        "UPDATE recipient_relays SET is_active=0, "
                        "notes=CONCAT(SUBSTRING(notes,1,200),' | probing') WHERE recipient_domain=%s",
                        (x["recipient_domain"],))
                    reopened += 1
            if not DRY_RUN:
                cnx.commit()
            emit("done", mode="own_nodes", dry_run=DRY_RUN, failed_over=moved, reopened=reopened,
                 candidates=candidates, failing_domains=failing)
            return 0

        # ---- 1) FAILOVER: recipient domains failing in the window --------------------
        cur.execute(
            """
            SELECT LOWER(SUBSTRING_INDEX(rcpt, '@', -1)) AS rdom,
                   SUM(status IN ('sent','delivered'))                  AS sent,
                   SUM(status='deferred')                               AS deferred,
                   SUM(status='bounced')                                AS bounced,
                   SUM(dsn LIKE '5.7.%%' OR dsn LIKE '4.7.%%')          AS block
            FROM delivery_events
            WHERE host LIKE %s
              AND ts >= NOW() - INTERVAL %s MINUTE
              AND rcpt LIKE '%%@%%'
            GROUP BY rdom
            HAVING COUNT(*) >= %s AND (SUM(status='deferred') + SUM(status='bounced')) > 0
            """,
            (POOL_HOST_LIKE, WINDOW_MIN, MIN_EVENTS),
        )
        rows = cur.fetchall()

        for r in rows:
            rdom = r["rdom"]
            d = decide(deferred=int(r["deferred"] or 0), bounced=int(r["bounced"] or 0),
                       sent=int(r["sent"] or 0), block=int(r["block"] or 0))
            if not d["failing"]:
                continue

            # respect a MANUAL pin (notes without the auto prefix): never overwrite it.
            cur.execute(
                "SELECT relay_id, is_active, notes FROM recipient_relays WHERE recipient_domain=%s",
                (rdom,))
            ex = cur.fetchone()
            if ex and int(ex["is_active"]) == 1 and not (ex["notes"] or "").startswith(AUTO_TAG):
                emit("skip_manual", rdom=rdom, **d)
                continue
            # already active on the overflow relay (auto) -> nothing to do.
            if ex and int(ex["is_active"]) == 1 and int(ex["relay_id"]) == overflow_id:
                continue

            note = (f"{AUTO_TAG} {d['reason']} blk={d['block_pct']}% "
                    f"def={d['defer_pct']}% n={d['total']} w={WINDOW_MIN}m")
            emit("failover", rdom=rdom, to="overflow", dry_run=DRY_RUN, **d)
            if not DRY_RUN:
                cur.execute(
                    """
                    INSERT INTO recipient_relays (recipient_domain, relay_id, is_active, notes)
                    VALUES (%s, %s, 1, %s)
                    ON DUPLICATE KEY UPDATE relay_id=VALUES(relay_id),
                        is_active=1, notes=VALUES(notes)
                    """,
                    (rdom, overflow_id, note),
                )
                moved += 1

        # ---- 2) AUTO-HEAL: reopen old auto rows to probe the original route again ------
        cur.execute(
            """
            SELECT recipient_domain FROM recipient_relays
            WHERE is_active=1 AND relay_id=%s AND notes LIKE %s
              AND updated_at <= NOW() - INTERVAL %s HOUR
            """,
            (overflow_id, AUTO_TAG + "%", COOLDOWN_H),
        )
        stale = [x["recipient_domain"] for x in cur.fetchall()]
        for rdom in stale:
            emit("reopen_probe", rdom=rdom, dry_run=DRY_RUN)
            if not DRY_RUN:
                cur.execute(
                    "UPDATE recipient_relays SET is_active=0, "
                    "notes=CONCAT(SUBSTRING(notes,1,200),' | probing') "
                    "WHERE recipient_domain=%s",
                    (rdom,))
                reopened += 1

        if not DRY_RUN:
            cnx.commit()

    emit("done", dry_run=DRY_RUN, failed_over=moved, reopened=reopened, candidates=len(rows))
    return 0


if __name__ == "__main__":
    sys.exit(main())
